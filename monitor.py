"""
Monitor de contingencia NF-e -> alerta no Slack.
Versao para rodar localmente no Windows via Agendador de Tarefas
(sem depender de GitHub Actions).

Cada execucao faz UMA verificacao e termina - o Agendador de Tarefas e
quem cuida de repetir isso a cada poucos minutos.

Dependencias: pip install requests beautifulsoup4
"""

import json
import os
import re
import signal
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- CONFIG

NFE_PRINCIPAL_URL = "https://www.nfe.fazenda.gov.br/portal/principal.aspx"
SVC_RS_URL = "https://www.sefaz.rs.gov.br/NFE/NFE-SVC.aspx"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": "AspxAutoDetectCookieSupport=1",
}

# UFs que a empresa monitora de fato (ajuste para as suas UFs de emissao)
UFS_MONITORADAS = {"SP", "MG", "PR", "RS", "BA", "AM", "GO", "MA", "MS", "MT", "PE"}

STATE_FILE = "state.json"

# URL do "trigger" do Slack Workflow Builder (a mesma que ja configuramos
# no #incident-fiscal). Fica aqui direto porque o arquivo vive so no seu
# notebook, nao num repositorio publico.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# URL do SEGUNDO fluxo do Workflow Builder (o do comunicado "ALERTA FISCAL | NF-e",
# que posta em outro canal). Cole aqui a URL do gatilho desse novo fluxo.
# Enquanto estiver vazio, o comunicado simplesmente nao e enviado.
SLACK_WEBHOOK_URL_COMUNICADO = os.environ.get("SLACK_WEBHOOK_URL_COMUNICADO", "")

# Link oficial de cada servico, usado no comunicado.
LINKS_SVC = {
    "SVC-RS": SVC_RS_URL,
    "SVC-AN": NFE_PRINCIPAL_URL,
}


class TempoExcedidoErro(Exception):
    """Erro proprio para forcar a interrupcao de uma chamada de rede presa."""


def _alarme(signum, frame):
    raise TempoExcedidoErro("Tempo limite absoluto excedido")


# ---------------------------------------------------------------- COLETA

def buscar_com_retry(url, tentativas=2, espera_segundos=3, limite_absoluto_segundos=20):
    """Busca a URL com ate 2 tentativas e um alarme de seguranca que
    interrompe na forca se travar (inclusive em resolucao de DNS)."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        # signal.alarm so existe em sistemas Unix - no Windows nao existe.
        # Aqui usamos so o timeout do requests mesmo (que ja vimos que
        # funciona bem quando testado antes).
        try:
            print(f"[DEBUG] Conectando em {url} (tentativa {tentativa})...", flush=True)
            resp = requests.get(url, headers=HEADERS, timeout=(10, 15))
            resp.encoding = "utf-8"  # ja confirmamos nos testes que os dois sites sao UTF-8
            print(f"[DEBUG] Resposta recebida de {url}: HTTP {resp.status_code}", flush=True)
            return resp
        except requests.RequestException as erro:
            ultimo_erro = erro
            print(f"[AVISO] Tentativa {tentativa}/{tentativas} falhou para {url}: {erro}", flush=True)
            if tentativa < tentativas:
                time.sleep(espera_segundos)
    raise ultimo_erro


def status_svc_an_nacional():
    resp = buscar_com_retry(NFE_PRINCIPAL_URL)
    texto = BeautifulSoup(resp.text, "html.parser").get_text("|")
    texto = re.sub(r"[ \t\r\n]+", " ", texto)

    ativas = set()
    agendadas = {}

    bloco_ativa = re.search(
        r"Contingência Ativada na SVC-AN(.*?)Contingência Agendada na SVC-AN",
        texto, re.IGNORECASE,
    )
    if bloco_ativa:
        trecho = bloco_ativa.group(1)
        if "não há" not in trecho.lower():
            for uf in re.findall(r"\b([A-Z]{2})\b", trecho):
                if uf in UFS_MONITORADAS:
                    ativas.add(uf)

    bloco_agendada = re.search(
        r"Contingência Agendada na SVC-AN(.*?)(Relação de UFs|Informes|$)",
        texto, re.IGNORECASE,
    )
    if bloco_agendada:
        trecho = bloco_agendada.group(1)
        if "não há" not in trecho.lower():
            padrao = re.compile(
                r"\b([A-Z]{2})\b\s*De\s+(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"
                r"\s+até\s+(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"
            )
            for uf, inicio, fim in padrao.findall(trecho):
                if uf in UFS_MONITORADAS:
                    agendadas[uf] = f"{inicio} até {fim}"

    return ativas, agendadas


def status_svc_rs():
    """Retorna (ativas, agendadas, ativas_janelas):
      ativas:         {UF: bool} - True/False para Ativada/Desativada
      agendadas:      {UF: janela} - para UFs com status "Agendada para ..."
      ativas_janelas: {UF: janela} - para UFs "Ativada em ... até ..." que ja
                      vem com a data de termino informada pelo proprio site
    """
    resp = buscar_com_retry(SVC_RS_URL)
    texto = BeautifulSoup(resp.text, "html.parser").get_text("|")

    # Em vez de uma regex complexa (que pode travar/demorar demais com
    # certos textos), dividimos o texto pelos "|" e comparamos pedaco por
    # pedaco - muito mais simples e nunca trava.
    partes = [p.strip() for p in texto.split("|")]
    partes = [p for p in partes if p]  # remove pedacos vazios

    padrao_uf = re.compile(r"^([A-Z]{2})\s*-\s*.+")
    padrao_janela_ativa = re.compile(r"^Ativada\s+em\s+(.+?)\s+até\s+(.+)$")
    ativas = {}
    agendadas = {}
    ativas_janelas = {}
    for i, parte in enumerate(partes):
        m = padrao_uf.match(parte)
        if not m:
            continue
        uf = m.group(1)
        if uf not in UFS_MONITORADAS:
            continue
        if i + 1 >= len(partes):
            continue
        proximo = partes[i + 1]
        if proximo.startswith("Ativada"):
            ativas[uf] = True
            # Ex: "Ativada em 22/09/2026 21:32 até 23/10/2026 23:59"
            mj = padrao_janela_ativa.match(proximo)
            if mj:
                ativas_janelas[uf] = f"{mj.group(1).strip()} até {mj.group(2).strip()}"
        elif proximo.startswith("Desativada"):
            ativas[uf] = False
        elif proximo.startswith("Agendada"):
            # Ex: "Agendada para  20/09/2026 00:05 até 20/09/2026 05:00"
            ativas[uf] = False
            janela = re.sub(r"^Agendada\s*(para)?\s*", "", proximo).strip()
            agendadas[uf] = janela
        # qualquer outro texto desconhecido: ignora essa UF nesta leitura,
        # em vez de travar - mas nao conta como ativa nem agendada.

    return ativas, agendadas, ativas_janelas


# ---------------------------------------------------------------- ESTADO

def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"svc_an": {}, "svc_an_agendadas": {}, "svc_rs": {}, "svc_rs_agendadas": {}}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- SLACK

def enviar_slack(uf, svc, situacao, desde="", ate=""):
    payload = {"UF": uf, "SVC": svc, "Situacao": situacao, "Horario": desde, "Ate": ate}
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
        print(f"[SLACK] {uf} {svc} {situacao} (desde {desde}, ate {ate}) -> HTTP {r.status_code}", flush=True)
    except requests.RequestException as erro:
        print(f"[FALHA SLACK] {uf} {svc} {situacao} -> {erro}", flush=True)


def _dia_hora(texto):
    """Extrai ("DD/MM", "HH:MM") de textos como "05/10/2026 10:00" ou
    "28/09/2026 07:57:29". Retorna None se nao houver data."""
    m = re.search(r"(\d{2}/\d{2})/\d{4}\s+(\d{2}:\d{2})", texto or "")
    return (m.group(1), m.group(2)) if m else None


def formatar_periodo(desde, ate):
    """Monta o trecho que entra depois de "do dia" no comunicado.
    Com fim conhecido:  15/10 das 13:22 ao dia 15/10 às 18:00
    Sem fim informado:  15/10 às 13:22 (sem previsão de término informada pela SEFAZ)"""
    ini = _dia_hora(desde)
    if not ini:
        return ""
    fim = _dia_hora(ate)
    if fim:
        return f"{ini[0]} das {ini[1]} ao dia {fim[0]} às {fim[1]}"
    return f"{ini[0]} às {ini[1]} (sem previsão de término informada pela SEFAZ)"


def enviar_comunicado(uf, svc, desde, ate):
    """Dispara o comunicado "ALERTA FISCAL | NF-e" (outro canal) quando uma
    contingencia fica ATIVA. O texto fixo mora no Workflow Builder; aqui vao
    so as variaveis: UF, Periodo e Link."""
    if not SLACK_WEBHOOK_URL_COMUNICADO:
        return
    payload = {
        "UF": uf,
        "Periodo": formatar_periodo(desde, ate),
        "Link": LINKS_SVC.get(svc, ""),
    }
    try:
        r = requests.post(SLACK_WEBHOOK_URL_COMUNICADO, json=payload, timeout=15)
        print(f"[COMUNICADO] {uf} {svc} ({payload['Periodo']}) -> HTTP {r.status_code}", flush=True)
    except requests.RequestException as erro:
        print(f"[FALHA COMUNICADO] {uf} {svc} -> {erro}", flush=True)


# ---------------------------------------------------------------- DECISAO

def checar_e_alertar():
    estado_anterior = carregar_estado()

    svc_an_antigo = estado_anterior.get("svc_an", {})          # {uf: {"ativa":bool,"desde":str}}
    agendadas_antigas = estado_anterior.get("svc_an_agendadas", {})
    svc_rs_antigo = estado_anterior.get("svc_rs", {})          # {uf: {"ativa":bool,"desde":str}}
    svc_rs_agendadas_antigas = estado_anterior.get("svc_rs_agendadas", {})

    agora = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")

    # Valores padrao: se uma fonte falhar, repete o que ja sabiamos.
    ativas_novas_set = {uf for uf, v in svc_an_antigo.items() if v.get("ativa")}
    agendadas_novas = agendadas_antigas
    svc_rs_novo_bruto = {uf: v.get("ativa", False) for uf, v in svc_rs_antigo.items()}
    svc_rs_agendadas_novas = svc_rs_agendadas_antigas
    svc_rs_janelas_ativas = {}

    try:
        print("[DEBUG] Iniciando leitura do Portal Nacional...", flush=True)
        ativas_novas_set, agendadas_novas = status_svc_an_nacional()
        print("[DEBUG] Portal Nacional lido com sucesso.", flush=True)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel ler o Portal Nacional agora: {erro}", flush=True)

    try:
        print("[DEBUG] Iniciando leitura do SVC-RS...", flush=True)
        svc_rs_novo_bruto, svc_rs_agendadas_novas, svc_rs_janelas_ativas = status_svc_rs()
        print("[DEBUG] SVC-RS lido com sucesso.", flush=True)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel ler o SVC-RS agora: {erro}", flush=True)

    # --- SVC-AN: ativacao/encerramento, com "desde" e "ate" ---
    svc_an_novo = {}
    for uf in ativas_novas_set:
        anterior = svc_an_antigo.get(uf, {})
        desde = anterior.get("desde") if anterior.get("ativa") else agora
        svc_an_novo[uf] = {"ativa": True, "desde": desde}
        if not anterior.get("ativa"):
            enviar_slack(uf, "SVC-AN", "ativada", desde=desde, ate="em andamento")
            enviar_comunicado(uf, "SVC-AN", desde, "em andamento")

    for uf, dados in svc_an_antigo.items():
        if dados.get("ativa") and uf not in ativas_novas_set:
            enviar_slack(uf, "SVC-AN", "encerrada", desde=dados.get("desde", ""), ate=agora)

    # --- SVC-AN: agendamentos (mesma logica de antes, ja tem janela completa) ---
    for uf, janela in agendadas_novas.items():
        if agendadas_antigas.get(uf) != janela:
            inicio, _, fim = janela.partition(" até ")
            enviar_slack(uf, "SVC-AN", f"agendada ({janela})", desde=inicio.strip(), ate=fim.strip())

    # --- SVC-RS por estado: ativacao/encerramento, com "desde" e "ate" ---
    svc_rs_novo = {}
    for uf, ativa in svc_rs_novo_bruto.items():
        anterior = svc_rs_antigo.get(uf, {})
        estava_ativa = anterior.get("ativa", False)
        if ativa:
            janela = svc_rs_janelas_ativas.get(uf)  # "DD/MM/AAAA HH:MM até DD/MM/AAAA HH:MM", se o site informar
            if janela:
                inicio, _, fim = janela.partition(" até ")
                desde = inicio.strip()
                ate_texto = fim.strip()
            else:
                desde = anterior.get("desde") if estava_ativa else agora
                ate_texto = "em andamento"
            svc_rs_novo[uf] = {"ativa": True, "desde": desde}
            # Se o site informa a janela oficial e o inicio mudou em relacao ao
            # que estava guardado, e uma contingencia NOVA - mesmo que o script
            # nao tenha visto a anterior terminar (ex.: notebook desligado).
            nova_janela = bool(janela) and estava_ativa and anterior.get("desde") != desde
            if not estava_ativa or nova_janela:
                enviar_slack(uf, "SVC-RS", "ativada", desde=desde, ate=ate_texto)
                enviar_comunicado(uf, "SVC-RS", desde, ate_texto)
        else:
            svc_rs_novo[uf] = {"ativa": False, "desde": None}
            if estava_ativa:
                enviar_slack(uf, "SVC-RS", "encerrada", desde=anterior.get("desde", ""), ate=agora)

    # --- SVC-RS: agendamentos ---
    for uf, janela in svc_rs_agendadas_novas.items():
        if svc_rs_agendadas_antigas.get(uf) != janela:
            inicio, _, fim = janela.partition(" até ")
            enviar_slack(uf, "SVC-RS", f"agendada ({janela})", desde=inicio.strip(), ate=fim.strip())

    novo_estado = {
        "svc_an": svc_an_novo,
        "svc_an_agendadas": agendadas_novas,
        "svc_rs": svc_rs_novo,
        "svc_rs_agendadas": svc_rs_agendadas_novas,
        "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
    }
    salvar_estado(novo_estado)
    print(f"[OK] Verificado em {novo_estado['ultima_verificacao']}", flush=True)


NUM_CHECKS_PER_RUN = 2
SECONDS_BETWEEN_CHECKS = 150


def main():
    print("[INICIO] Script comecou a rodar.", flush=True)
    for i in range(NUM_CHECKS_PER_RUN):
        try:
            checar_e_alertar()
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] Falha na verificacao: {erro}", flush=True)
        if i < NUM_CHECKS_PER_RUN - 1:
            time.sleep(SECONDS_BETWEEN_CHECKS)


if __name__ == "__main__":
    main()
