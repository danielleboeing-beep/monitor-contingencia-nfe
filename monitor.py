"""
Monitor de contingencia NF-e -> alerta no Slack (sem custo, via GitHub Actions).

Fluxo:
  1. Coleta   -> le o Portal Nacional NF-e (SVC-AN ativada/agendada)
                 e o Painel SVC-RS por estado (SEFAZ/RS)
  2. Decisao  -> compara com o ultimo estado salvo em state.json
  3. Alerta   -> so dispara no Slack quando o estado MUDA (transicao),
                 nunca repete enquanto o estado for igual ao anterior
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

# UFs que a empresa monitora de fato (ajuste para as suas UFs de emissao)
UFS_MONITORADAS = {"SP", "MG", "PR", "RS", "BA", "AM", "GO", "MA", "MS", "MT", "PE"}

STATE_FILE = "state.json"

# URL do "trigger" do Slack Workflow Builder (guardada como secret no
# GitHub -> Settings -> Secrets and variables -> Actions -> SLACK_WEBHOOK_URL)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Quantas checagens fazer dentro de uma unica execucao do workflow, e o
# intervalo entre elas. 2 checagens x 150s = cobre ~5 minutos, aproximando
# de uma verificacao a cada 2min30s (o minimo real do GitHub Actions
# agendado e 5 em 5 minutos).
NUM_CHECKS_PER_RUN = 2
SECONDS_BETWEEN_CHECKS = 150


# ---------------------------------------------------------------- COLETA

def buscar_com_retry(url, tentativas=3, espera_segundos=5):
    """Busca a URL com ate 3 tentativas, para nao derrubar tudo por causa
    de uma instabilidade passageira do site (comum em portais de governo)."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp
        except requests.RequestException as erro:
            ultimo_erro = erro
            print(f"[AVISO] Tentativa {tentativa}/{tentativas} falhou para {url}: {erro}")
            if tentativa < tentativas:
                time.sleep(espera_segundos)
    raise ultimo_erro


def status_svc_an_nacional():
    """
    Le o Portal Nacional e retorna:
      ativas:    set de UFs com SVC-AN ativada agora
      agendadas: dict {UF: "DD/MM/AAAA HH:MM:SS a DD/MM/AAAA HH:MM:SS"}
    """
    resp = buscar_com_retry(NFE_PRINCIPAL_URL)
    texto = BeautifulSoup(resp.text, "html.parser").get_text("|")
    texto = re.sub(r"[ \t\r\n]+", " ", texto)

    ativas = set()
    agendadas = {}

    # --- Ativada na SVC-AN ---
    bloco_ativa = re.search(
        r"Contingência Ativada na SVC-AN(.*?)Contingência Agendada na SVC-AN",
        texto,
        re.IGNORECASE,
    )
    if bloco_ativa:
        trecho = bloco_ativa.group(1)
        if "não há" not in trecho.lower():
            for uf in re.findall(r"\b([A-Z]{2})\b", trecho):
                if uf in UFS_MONITORADAS:
                    ativas.add(uf)

    # --- Agendada na SVC-AN ---
    bloco_agendada = re.search(
        r"Contingência Agendada na SVC-AN(.*?)(Relação de UFs|Informes|$)",
        texto,
        re.IGNORECASE,
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
    """Retorna {UF: (ativa: bool, detalhe: str)} do painel SVC-RS por estado."""
    resp = buscar_com_retry(SVC_RS_URL)
    texto = BeautifulSoup(resp.text, "html.parser").get_text("|")
    texto = re.sub(r"[ \t\r\n]+", " ", texto)
    # A pagina real tem espaco entre os separadores ("| |"), por isso o
    # conector precisa aceitar um-ou-mais "|" com espacos entre eles,
    # nao so "|" colados.
    padrao = r"\b([A-Z]{2})\s*-\s*[^|]*(?:\s*\|\s*)+(Ativada[^|]*|Desativada)"
    return {
        uf: (det.strip().startswith("Ativada"), det.strip())
        for uf, det in re.findall(padrao, texto)
        if uf in UFS_MONITORADAS
    }


# ---------------------------------------------------------------- ESTADO

def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"svc_an_ativas": [], "svc_an_agendadas": {}, "svc_rs": {}}


def salvar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- SLACK

def enviar_slack(uf, svc, situacao):
    if not SLACK_WEBHOOK_URL:
        print("[AVISO] SLACK_WEBHOOK_URL nao configurado - alerta nao enviado.")
        return

    horario = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")

    # Mandamos o campo em varias grafias (uf/UF, svc/SVC...) porque o nome
    # exato da variavel no Workflow Builder do Slack pode ter sido salvo
    # com letra maiuscula ou minuscula - depois de confirmar qual o Slack
    # espera, pode limpar e deixar so uma versao.
    payload = {
        "uf": uf, "UF": uf,
        "svc": svc, "SVC": svc,
        "situacao": situacao, "Situacao": situacao,
        "horario": horario, "Horario": horario,
    }

    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
        print(f"[SLACK] {uf} {svc} {situacao} -> HTTP {r.status_code}")
    except requests.RequestException as erro:
        print(f"[FALHA SLACK] {uf} {svc} {situacao} -> {erro}")


# ---------------------------------------------------------------- DECISAO

def checar_e_alertar():
    estado_anterior = carregar_estado()

    ativas_antigas = set(estado_anterior.get("svc_an_ativas", []))
    agendadas_antigas = estado_anterior.get("svc_an_agendadas", {})
    svc_rs_antigo = estado_anterior.get("svc_rs", {})

    # Valores padrao: se uma fonte falhar mesmo depois das tentativas,
    # repete o ultimo estado conhecido dela em vez de travar tudo ou
    # zerar o que ja sabiamos.
    ativas_novas, agendadas_novas = ativas_antigas, agendadas_antigas
    svc_rs_novo = {uf: (v.get("ativa", False), "") for uf, v in svc_rs_antigo.items()}

    try:
        ativas_novas, agendadas_novas = status_svc_an_nacional()
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel ler o Portal Nacional agora: {erro}")

    try:
        svc_rs_novo = status_svc_rs()
    except Exception as erro:  # noqa: BLE001
        print(f"[ERRO] Nao foi possivel ler o SVC-RS agora: {erro}")

    # --- SVC-AN ativada: transicoes ---
    for uf in ativas_novas - ativas_antigas:
        enviar_slack(uf, "SVC-AN", "ativada")
    for uf in ativas_antigas - ativas_novas:
        enviar_slack(uf, "SVC-AN", "encerrada")

    # --- SVC-AN agendada: transicoes (novo agendamento aparecendo) ---
    for uf, janela in agendadas_novas.items():
        if agendadas_antigas.get(uf) != janela:
            enviar_slack(uf, "SVC-AN", f"agendada ({janela})")

    # --- SVC-RS por estado: transicoes ---
    for uf, (ativa, _detalhe) in svc_rs_novo.items():
        estava_ativa = svc_rs_antigo.get(uf, {}).get("ativa", False)
        if ativa and not estava_ativa:
            enviar_slack(uf, "SVC-RS", "ativada")
        elif not ativa and estava_ativa:
            enviar_slack(uf, "SVC-RS", "encerrada")

    novo_estado = {
        "svc_an_ativas": sorted(ativas_novas),
        "svc_an_agendadas": agendadas_novas,
        "svc_rs": {uf: {"ativa": ativa} for uf, (ativa, _) in svc_rs_novo.items()},
        "ultima_verificacao": datetime.now(timezone.utc).isoformat(),
    }
    salvar_estado(novo_estado)
    print(f"[OK] Verificado em {novo_estado['ultima_verificacao']}")


# ---------------------------------------------------------------- MAIN

def main():
    for i in range(NUM_CHECKS_PER_RUN):
        try:
            checar_e_alertar()
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] Falha na verificacao: {erro}")

        if i < NUM_CHECKS_PER_RUN - 1:
            time.sleep(SECONDS_BETWEEN_CHECKS)


if __name__ == "__main__":
    main()
