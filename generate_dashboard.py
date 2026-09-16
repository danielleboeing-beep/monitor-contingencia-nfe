"""
Gera docs/index.html (o painel) a partir do state.json atual.
Chamado pelo workflow do GitHub Actions depois de monitor.py rodar.
"""

import json
import os
from datetime import datetime

STATE_FILE = "state.json"
OUT_DIR = "docs"
OUT_FILE = os.path.join(OUT_DIR, "index.html")

UFS_MONITORADAS = ["SP", "MG", "PR", "RS", "BA", "AM", "GO", "MA", "MS", "MT", "PE"]


def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"svc_an_ativas": [], "svc_an_agendadas": {}, "svc_rs": {}, "ultima_verificacao": None}


def linha_svc_rs(uf, ativo):
    cor = "danger" if ativo else "ok"
    texto = "Ativada" if ativo else "Desativada"
    return f"""
        <tr>
          <td><span class="uf-code">{uf}</span></td>
          <td><span class="pill {cor}">{texto}</span></td>
        </tr>"""


def gerar_html(estado):
    ativas = estado.get("svc_an_ativas", [])
    agendadas = estado.get("svc_an_agendadas", {})
    svc_rs = estado.get("svc_rs", {})
    ultima = estado.get("ultima_verificacao") or "nunca"

    tem_ativa = len(ativas) > 0
    tem_agendada = len(agendadas) > 0
    tem_svc_rs_ativa = any(v.get("ativa") for v in svc_rs.values())
    tudo_normal = not (tem_ativa or tem_agendada or tem_svc_rs_ativa)

    status_geral = (
        '<span class="pill ok">NORMAL — nenhuma contingência ativa</span>'
        if tudo_normal
        else '<span class="pill danger">ATENÇÃO — existe contingência ativa ou agendada</span>'
    )

    linhas_ativas = (
        "".join(f'<li><span class="uf-code">{uf}</span> — SVC-AN ativada agora</li>' for uf in ativas)
        or '<div class="empty-note">Não há estados com SVC-AN ativa no momento.</div>'
    )

    linhas_agendadas = (
        "".join(
            f'<li><span class="uf-code">{uf}</span> — janela: {janela}</li>'
            for uf, janela in agendadas.items()
        )
        or '<div class="empty-note">Não há agendamentos de SVC-AN.</div>'
    )

    linhas_svc_rs = "".join(
        linha_svc_rs(uf, svc_rs.get(uf, {}).get("ativa", False)) for uf in UFS_MONITORADAS
    )

    gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="120">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Painel de Contingência NF-e</title>
<style>
  :root{{
    --bg:#0f1620; --panel:#16202c; --line:#29394a; --text:#e7edf3; --text-dim:#8ea0b3;
    --ok:#35c48a; --ok-dim:#16302a; --danger:#e0554d; --danger-dim:#35201e;
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }}
  *{{box-sizing:border-box;}}
  body{{margin:0;background:var(--bg);color:var(--text);padding:28px 20px 50px;max-width:900px;margin:0 auto;}}
  h1{{font-size:22px;margin:0 0 4px;}}
  .sub{{color:var(--text-dim);font-size:13px;margin-bottom:24px;}}
  section{{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:18px;padding:18px 20px;}}
  section h2{{font-size:15px;margin:0 0 12px;}}
  .pill{{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;}}
  .pill.ok{{background:var(--ok-dim);color:var(--ok);}}
  .pill.danger{{background:var(--danger-dim);color:var(--danger);}}
  .uf-code{{font-weight:650;background:#1c2836;border-radius:5px;padding:2px 7px;font-size:12px;}}
  ul{{list-style:none;padding:0;margin:0;font-size:13.5px;line-height:2;}}
  table{{width:100%;border-collapse:collapse;font-size:13px;}}
  td{{padding:8px 4px;border-bottom:1px solid var(--line);}}
  .empty-note{{color:var(--text-dim);font-size:13.5px;}}
  footer{{text-align:center;color:var(--text-dim);font-size:11.5px;margin-top:20px;}}
</style>
</head>
<body>
  <h1>Painel de Contingência — NF-e</h1>
  <div class="sub">Atualizado automaticamente pelo GitHub Actions · última verificação SEFAZ: {ultima} · página gerada em {gerado_em}</div>

  <section>
    <h2>Status geral</h2>
    {status_geral}
  </section>

  <section>
    <h2>SVC-AN ativada agora</h2>
    <ul>{linhas_ativas}</ul>
  </section>

  <section>
    <h2>SVC-AN agendada</h2>
    <ul>{linhas_agendadas}</ul>
  </section>

  <section>
    <h2>SVC-RS por estado</h2>
    <table>{linhas_svc_rs}</table>
  </section>

  <footer>Fontes: Portal Nacional NF-e e Painel SVC-RS (SEFAZ/RS) · alerta automático no Slack em transições de estado</footer>
</body>
</html>"""

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] Painel gerado em {OUT_FILE}")


if __name__ == "__main__":
    gerar_html(carregar_estado())
