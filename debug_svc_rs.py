"""
Script de debug: mostra, direto no log do GitHub Actions, o texto que o
monitor.py realmente usa para tentar reconhecer cada UF - sem precisar
baixar nenhum arquivo.
"""

import re

import requests
from bs4 import BeautifulSoup

URL = "https://www.sefaz.rs.gov.br/NFE/NFE-SVC.aspx"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": "AspxAutoDetectCookieSupport=1",
}

resp = requests.get(URL, headers=HEADERS, timeout=30)
resp.encoding = resp.apparent_encoding or "utf-8"  # deixa o requests detectar certo

print(f"HTTP status: {resp.status_code}")
print(f"Encoding detectado: {resp.encoding}")
print(f"Tamanho do HTML: {len(resp.text)} caracteres")
print("=" * 60)

texto = BeautifulSoup(resp.text, "html.parser").get_text("|")
texto_limpo = re.sub(r"[ \t\r\n]+", " ", texto)

print("TEXTO PROCESSADO (o que o monitor.py realmente ve, primeiros 4000 caracteres):")
print("=" * 60)
print(texto_limpo[:4000])
print("=" * 60)

padrao = r"\b([A-Z]{2})\s*-\s*[^|]*\|+\s*(Ativada[^|]*|Desativada)"
achados = re.findall(padrao, texto_limpo)
print(f"REGEX ATUAL encontrou {len(achados)} UFs:")
for uf, status in achados:
    print(f"  {uf} -> {status.strip()}")

if not achados:
    print("Nenhuma UF reconhecida - o padrao (regex) nao bate com o texto real.")
