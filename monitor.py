"""
Monitor de contingencia NF-e -> alerta no Slack (via GitHub Actions).

Fluxo:
  1. Coleta   -> le o Portal Nacional NF-e (SVC-AN ativada/agendada)
                 e o Painel SVC-RS por estado (SEFAZ/RS)
  2. Decisao  -> compara com o ultimo estado salvo em state.json,
                 rastreando desde-quando cada UF ficou ativa
  3. Alerta   -> so dispara no Slack quando o estado MUDA (transicao),
                 informando "desde" e "ate" (quando encerra)
  4. Registro -> grava o novo estado em state.json (comitado de volta
                 pro repositorio pelo workflow do GitHub Actions)

Dependencias: pip install requests beautifulsoup4
"""

import json
import os
import re
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

UFS_MONITORADAS = {"SP", "MG", "PR", "RS", "BA", "AM", "GO", "MA", "MS", "MT", "PE"}

STATE_FILE = "state.json"

# URL do "trigger" do Slack Workflow Builder (Settings -> Secrets and
# variables -> Actions -> SLACK_WEBHOOK_URL)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

NUM_CHECKS_PER_RUN = 2
SECONDS_BETWEEN_CHECKS = 150


# ---------------------------------------------------------------- COLETA

def buscar_com_retry(url, tentativas=2, espera_segundos=3):
    """Busca a URL com ate 2 tentativas. O timeout do requests ja se
    mostrou confiavel nos testes - o travamento anterior era um bug de
    regex (catastrophic backtracking), nao rede."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            print(f"[DEBUG] Conectando em {url} (tentativa {tentativa})...", flush=True)
            resp = requests.get(url, headers=HEADERS, timeout=(10, 15))
            resp.encoding = "utf-8"
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
    """Retorna {UF: (ativa: bool, detalhe: str)}. Divide o texto por '|'
    e compara pedaco a pedaco - evita a regex complexa que travava."""
    resp = buscar_com_retry(SVC_RS_URL)
    texto = BeautifulSoup(resp.text, "html.parser").get_text("|")

    partes = [p.strip() for p in texto.split("|")]
    partes = [p for p in partes if p]

    padrao_uf = re.compile(r"^([A-Z]{2})\s*-\s*.+")
    resultado = {}
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
        if proximo.startswith("Ativada") or proximo.startswith("Desativada"):
            resultado[uf] = (proximo.startswith("Ativada"), proximo)

    return resultado


# ---------------------------------------------------------------- ESTADO

def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"svc_an": {}, "svc_an_agendadas": {}, "svc_rs": {}}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- SLACK

def enviar_slack(uf, svc, situacao, desde="", ate=""):
    if not SLACK_WEBHOOK_URL:
        print("[AVISO] SLACK_WEBHOOK_URL nao configurado - alerta nao enviado.", flush=True)
        return
    payload = {"UF": uf, "SVC": svc, "Situacao": situacao, "Horario": desde, "Ate": ate}
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
        print(f"[SLACK] {uf} {svc} {situacao} (desde {desde}, ate {ate}) -> HTTP {r.status_code}", flush=True)
    except requests.RequestException as erro:
        print(f"[FALHA SLACK] {uf} {svc} {situacao} -> {erro}", flush=True)


# ---------------------------------------------------------------- DECISAO

def checar_e_alertar():
    estado_anterior = carregar_estado()

    svc_an_antigo = estado_anterior.get("svc_an", {})
    agendadas_antigas = estado_anterior.get("svc_an_agendadas", {})
    svc_rs_antigo = estado_anterior.get("svc_rs", {})

    agora = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")

    ativas_novas_set = {uf for uf, v in svc_an_antigo.items() if v.get("ativa")}
    agendadas_novas = agendadas_antigas
    svc_rs_novo_bruto = {uf: v.get("ativa", False) for uf, v in svc_rs_antigo.items()}

    try:
        print("[DEBUG] Iniciando leitura do Portal Nacional...", flush=True)
        ativas_novas_set, agendadas_novas = status_svc_an_nacional()
        print("[DEBUG] Portal Nacional lido com sucesso.", flush=True)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel ler o Portal Nacional agora: {erro}", flush=True)

    try:
        print("[DEBUG] Iniciando leitura do SVC-RS...", flush=True)
        svc_rs_check = status_svc_rs()
        svc_rs_novo_bruto = {uf: ativa for uf, (ativa, _det) in svc_rs_check.items()}
        print("[DEBUG] SVC-RS lido com sucesso.", flush=True)
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel ler o SVC-RS agora: {erro}", flush=True)

    svc_an_novo = {}
    for uf in ativas_novas_set:
        anterior = svc_an_antigo.get(uf, {})
        desde = anterior.get("desde") if anterior.get("ativa") else agora
        svc_an_novo[uf] = {"ativa": True, "desde": desde}
        if not anterior.get("ativa"):
            enviar_slack(uf, "SVC-AN", "ativada", desde=desde, ate="em andamento")

    for uf, dados in svc_an_antigo.items():
        if dados.get("ativa") and uf not in ativas_novas_set:
            enviar_slack(uf, "SVC-AN", "encerrada", desde=dados.get("desde", ""), ate=agora)

    for uf, janela in agendadas_novas.items():
        if agendadas_antigas.get(uf) != janela:
            enviar_slack(uf, "SVC-AN", f"agendada ({janela})")

    svc_rs_novo = {}
    for uf, ativa in svc_rs_novo_bruto.items():
        anterior = svc_rs_antigo.get(uf, {})
        estava_ativa = anterior.get("ativa", False)
        if ativa:
            desde = anterior.get("desde") if estava_ativa else agora
            svc_rs_novo[uf] = {"ativa": True, "desde": desde}
            if not estava_ativa:
                enviar_slack(uf, "SVC-RS", "ativada", desde=desde, ate="em andamento")
        else:
            svc_rs_novo[uf] = {"ativa": False, "desde": None}
            if estava_ativa:
                enviar_slack(uf, "SVC-RS", "encerrada", desde=anterior.get("desde", ""), ate=agora)

    novo_estado = {
        "svc_an": svc_an_novo,
        "svc_an_agendadas": agendadas_novas,
        "svc_rs": svc_rs_novo,
        "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
    }
    salvar_estado(novo_estado)
    print(f"[OK] Verificado em {novo_estado['ultima_verificacao']}", flush=True)


# ---------------------------------------------------------------- MAIN

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
