"""
Script temporario so para inspecionar o HTML real da pagina do SVC-RS.
Salva o conteudo bruto em debug_svc_rs.html para conseguirmos ver a
estrutura de verdade e corrigir o regex de leitura no monitor.py.
"""

import requests

URL = "https://www.sefaz.rs.gov.br/NFE/NFE-SVC.aspx"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": "AspxAutoDetectCookieSupport=1",
}

resp = requests.get(URL, headers=HEADERS, timeout=30)
resp.encoding = "latin-1"

with open("debug_svc_rs.html", "w", encoding="utf-8") as f:
    f.write(resp.text)

print(f"HTTP status: {resp.status_code}")
print(f"Tamanho do conteudo: {len(resp.text)} caracteres")
