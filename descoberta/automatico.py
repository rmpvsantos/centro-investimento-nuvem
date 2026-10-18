"""
Pesquisas automáticas: critérios guardados que correm sozinhos e avisam quando aparece algo novo.

- Guardar: pesquisas/<nome>.json com os critérios (pesquisa.py) e as classificações que dão alerta.
- Correr: python automatico.py --pesquisa <nome>   (ou --todas). É isto que o Agendador de Tarefas do
  Windows chama, sem a app aberta.
- Cada execução fica em resultados/automaticas/<nome>/, é comparada com a anterior e gera um relatório
  HTML (tema escuro dos relatórios). Se entrar uma empresa nova nas classificações de alerta, aparece
  uma notificação do Windows.
- Agendar: ativar_agendamento() cria uma tarefa no Agendador de Tarefas do Windows (schtasks), só quando
  o Rui carrega no botão da app.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

from pesquisa import descricao, executar

PASTA = Path(__file__).resolve().parent
PASTA_PESQUISAS = PASTA / "pesquisas"
PASTA_AUTOMATICAS = PASTA / "resultados" / "automaticas"
PREFIXO_TAREFA = "Descoberta de Acoes - "
CLASSES_ALERTA_POR_OMISSAO = ["Bom negócio a preço atrativo"]


# ---------------------------------------------------------------------------
# Pesquisas guardadas
# ---------------------------------------------------------------------------
def identificador(nome: str) -> str:
    """Nome de ficheiro e de tarefa seguro: sem acentos nem espaços."""
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower() or "pesquisa"


def guardar(nome: str, criterios: dict, alertar: list[str]) -> str:
    PASTA_PESQUISAS.mkdir(parents=True, exist_ok=True)
    ident = identificador(nome)
    anterior = carregar(ident) or {}
    registo = {"nome": nome, "id": ident, "criterios": criterios, "alertar": alertar,
               "criada": anterior.get("criada", datetime.now().isoformat(timespec="minutes")),
               "alterada": datetime.now().isoformat(timespec="minutes"),
               "ultima_execucao": anterior.get("ultima_execucao")}
    (PASTA_PESQUISAS / f"{ident}.json").write_text(json.dumps(registo, ensure_ascii=False, indent=2), encoding="utf-8")
    return ident


def carregar(ident: str) -> dict | None:
    f = PASTA_PESQUISAS / f"{ident}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def listar() -> list[dict]:
    if not PASTA_PESQUISAS.exists():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(PASTA_PESQUISAS.glob("*.json"))]


def apagar(ident: str) -> None:
    desativar_agendamento(ident)
    (PASTA_PESQUISAS / f"{ident}.json").unlink(missing_ok=True)


def _atualizar_ultima(ident: str, resumo: dict) -> None:
    reg = carregar(ident)
    if reg:
        reg["ultima_execucao"] = resumo
        (PASTA_PESQUISAS / f"{ident}.json").write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Execução, comparação e relatório
# ---------------------------------------------------------------------------
def execucoes(ident: str) -> list[Path]:
    pasta = PASTA_AUTOMATICAS / ident
    return sorted(pasta.glob("*.pkl")) if pasta.exists() else []


def _em_alerta(df: pd.DataFrame, classes: list[str]) -> set[str]:
    if df is None or df.empty:
        return set()
    return set(df.loc[df["Classificação"].isin(classes), "Ticker"])


def correr(ident: str, progresso=None, notificar: bool = True) -> dict:
    reg = carregar(ident)
    if not reg:
        raise ValueError(f"Não existe a pesquisa '{ident}'.")
    classes = reg.get("alertar") or CLASSES_ALERTA_POR_OMISSAO
    anteriores = execucoes(ident)
    df_ant = pd.read_pickle(anteriores[-1]) if anteriores else None

    r = executar(reg["criterios"], progresso=progresso)
    df = r["df"]
    agora = datetime.now()
    pasta = PASTA_AUTOMATICAS / ident
    pasta.mkdir(parents=True, exist_ok=True)
    base = pasta / f"{agora:%Y-%m-%d_%H%M%S}"
    (df if not df.empty else pd.DataFrame(columns=["Ticker", "Classificação"])).to_pickle(base.with_suffix(".pkl"))

    atuais, antes = _em_alerta(df, classes), _em_alerta(df_ant, classes)
    novas = sorted(atuais - antes) if df_ant is not None else sorted(atuais)
    sairam = sorted(antes - atuais)
    relatorio = _escrever_relatorio(reg, r, classes, novas, sairam, df_ant is None, base.with_suffix(".html"))
    (pasta / "ultimo.html").write_text(relatorio.read_text(encoding="utf-8"), encoding="utf-8")

    resumo = {"quando": agora.isoformat(timespec="minutes"), "universo": r["universo"],
              "analisadas": r["analisadas"], "passaram": len(df), "em_alerta": len(atuais),
              "novas": novas, "sairam": sairam, "falhas": len(r["falhas"]), "relatorio": str(relatorio)}
    # Valores de todas as analisadas e medianas de cada grupo (a página da Pesquisa recalcula na hora quando se mudam
    # os critérios, sem nova procura)
    (pasta / "medianas.json").unlink(missing_ok=True)  # ficheiro da 1.ª versão (substituído por resultado.json)
    if r.get("resultado"):
        (pasta / "resultado.json").write_text(json.dumps(r["resultado"], ensure_ascii=False, default=str), encoding="utf-8")
        resumo["nota"] = r["resultado"].get("nota")
    _atualizar_ultima(ident, resumo)
    if notificar and novas:
        nomes = ", ".join(novas[:6]) + ("..." if len(novas) > 6 else "")
        notificar_windows(f"{reg['nome']}: {len(novas)} oportunidade(s) nova(s)", nomes)
    return resumo


_CSS = """
:root{--bg:#0b1220;--panel:#111a2e;--line:#22304a;--ink:#e8edf7;--dim:#a9b6cc;--accent:#4fd1c5;--accent2:#f2b134;
--good:#3ecf8e;--bad:#ef5b5b;--font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);line-height:1.55;font-size:15px}
main{max-width:1150px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:1.7rem;margin:0 0 4px} h1 span{color:var(--accent)} h2{margin-top:32px;font-size:1.2rem}
.sub{color:var(--dim);font-size:.9rem}
.grelha{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 16px}
.kpi b{display:block;font-size:1.5rem} .kpi small{color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-size:.7rem}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:.88rem}
th{color:var(--dim);font-weight:600;background:#0e1626} td.n{text-align:right;font-variant-numeric:tabular-nums}
.nova{color:var(--accent2);font-weight:700} .bom{color:var(--good)} .mau{color:var(--bad)}
.caixa{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--accent2);border-radius:12px;
padding:12px 16px;margin:14px 0} .nota{color:var(--dim);font-size:.82rem;margin-top:28px}
"""


def _fmt(v, tipo="num"):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    if tipo == "pct":
        return f"{v * 100:+.0f}%".replace(".", ",")
    if tipo == "nota":
        return f"{v:.0f}"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _escrever_relatorio(reg, r, classes, novas, sairam, primeira, destino: Path) -> Path:
    df = r["df"]
    e = html.escape
    em_alerta = df[df["Classificação"].isin(classes)] if not df.empty else df
    linhas = []
    for _, x in em_alerta.iterrows():
        nova = x["Ticker"] in novas
        linhas.append(
            f"<tr><td class='{'nova' if nova else ''}'>{e(str(x['Ticker']))}{' ★ nova' if nova else ''}</td>"
            f"<td>{e(str(x['Empresa']))}</td><td>{e(str(x.get('País') or ''))}</td><td>{e(str(x['Classificação']))}</td>"
            f"<td class='n'>{_fmt(x.get('Qualidade'), 'nota')}</td><td class='n'>{_fmt(x.get('Preço (nota)'), 'nota')}</td>"
            f"<td>{e(str(x.get('Moat') or '—'))}</td>"
            f"<td class='n'>{_fmt(x.get('Preço atual'))} {e(str(x.get('Moeda') or ''))}</td>"
            f"<td class='n'>{_fmt(x.get('Valor esperado'))}</td><td class='n'>{_fmt(x.get('Preço de entrada'))}</td>"
            f"<td class='n {'bom' if (x.get('Desconto') or 0) > 0 else 'mau'}'>{_fmt(x.get('Desconto'), 'pct')}</td>"
            f"<td>{e(str(x.get('Critérios') or '—'))}</td>"
            f"<td class='nova'>{e(str(x.get('Não cumpriu') or ''))}</td></tr>")
    tabela = ("<table><tr><th>Ticker</th><th>Empresa</th><th>País</th><th>Classificação</th><th>Qualidade</th>"
              "<th>Preço</th><th>Moat</th><th>Preço atual</th><th>Valor esperado</th><th>Preço de entrada</th>"
              "<th>Desconto</th><th>Critérios</th><th>Não cumpriu</th></tr>" + "".join(linhas) + "</table>") if linhas else \
        "<p class='sub'>Nenhuma empresa nas classificações de alerta nesta execução.</p>"
    if primeira:
        caixa = "<div class='caixa'>Primeira execução desta pesquisa: todas as empresas em alerta contam como novas.</div>"
    elif novas or sairam:
        caixa = ("<div class='caixa'>" + (f"<b>Entraram:</b> {e(', '.join(novas))}. " if novas else "")
                 + (f"<b>Saíram:</b> {e(', '.join(sairam))}." if sairam else "") + "</div>")
    else:
        caixa = "<div class='caixa'>Sem alterações face à execução anterior.</div>"
    nota_res = (r.get("resultado") or {}).get("nota")
    bloco_med = f"<div class='caixa'>{e(nota_res)}</div>" if nota_res else ""
    agora = datetime.now()
    conteudo = f"""<!doctype html><html lang="pt-PT"><head><meta charset="utf-8">
<title>{e(reg['nome'])} · {agora:%d/%m/%Y}</title><style>{_CSS}</style></head><body><main>
<h1>Pesquisa automática · <span>{e(reg['nome'])}</span></h1>
<div class="sub">{agora:%d/%m/%Y às %H:%M} · alertas para: {e(', '.join(classes))}</div>
<div class="grelha">
<div class="kpi"><small>Universo (filtros do Yahoo)</small><b>{r['universo']}</b></div>
<div class="kpi"><small>Analisadas</small><b>{r['analisadas']}</b></div>
<div class="kpi"><small>Passam todos os filtros</small><b>{len(df)}</b></div>
<div class="kpi"><small>Em alerta</small><b>{len(em_alerta)}</b></div>
<div class="kpi"><small>Novas</small><b class="nova">{len(novas)}</b></div>
</div>
{caixa}
<h2>Empresas em alerta</h2>
{tabela}
{bloco_med}
<h2>Critérios</h2>
<p class="sub">{e(r.get('descricao') or descricao(reg['criterios']))}</p>
<p class="nota">Gerado pela app Descoberta de Ações com dados do Yahoo Finance. É uma triagem, não uma recomendação
de investimento: confirma sempre nos relatórios da empresa antes de decidir. {len(r['falhas'])} empresa(s) sem análise.</p>
</main></body></html>"""
    destino.write_text(conteudo, encoding="utf-8")
    return destino


# ---------------------------------------------------------------------------
# Notificação do Windows (sem módulos extra: usa a API de notificações através do PowerShell)
# ---------------------------------------------------------------------------
def notificar_windows(titulo: str, texto: str) -> bool:
    def ps(s: str) -> str:
        return s.replace("'", "''")

    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t = $x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode('{ps(titulo)}')) | Out-Null;"
        f"$t.Item(1).AppendChild($x.CreateTextNode('{ps(texto)}')) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:  # noqa: BLE001 — a notificação é um extra; o relatório fica gravado na mesma
        return False


# ---------------------------------------------------------------------------
# Agendador de Tarefas do Windows
# ---------------------------------------------------------------------------
DIAS_SEMANA = {"segunda": "MON", "terça": "TUE", "quarta": "WED", "quinta": "THU", "sexta": "FRI", "sábado": "SAT",
               "domingo": "SUN"}


def nome_tarefa(ident: str) -> str:
    return PREFIXO_TAREFA + ident


def _pythonw() -> str:
    candidato = Path(sys.executable).with_name("pythonw.exe")  # corre sem abrir janela de consola
    return str(candidato if candidato.exists() else Path(sys.executable))


def ativar_agendamento(ident: str, frequencia: str, hora: str, dia: str = "segunda") -> tuple[bool, str]:
    """frequencia: 'diária', 'dias úteis' ou 'semanal'; hora: 'HH:MM'."""
    if sys.platform != "win32":
        return False, "na nuvem a procura automática é às segundas-feiras às 18:00 (marcada no GitHub)"
    comando = f'"{_pythonw()}" "{PASTA / "automatico.py"}" --pesquisa {ident}'
    args = ["schtasks", "/Create", "/F", "/TN", nome_tarefa(ident), "/TR", comando, "/ST", hora]
    if frequencia == "semanal":
        args += ["/SC", "WEEKLY", "/D", DIAS_SEMANA[dia]]
    elif frequencia == "dias úteis":
        args += ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI"]
    else:
        args += ["/SC", "DAILY"]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def desativar_agendamento(ident: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "na nuvem a procura automática é marcada no GitHub"
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", nome_tarefa(ident)], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def _agendamento_nuvem() -> dict:
    """Na nuvem (Centro de Investimento no GitHub) a procura é às segundas às 18:00 de Lisboa, marcada no próprio GitHub."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    agora = datetime.now(ZoneInfo("Europe/Lisbon"))
    prox = (agora + timedelta(days=(7 - agora.weekday()) % 7)).replace(hour=18, minute=0, second=0, microsecond=0)
    if prox <= agora:
        prox += timedelta(days=7)
    return {"proxima": prox.strftime("%d/%m/%Y %H:%M"), "ultima": "", "resultado": "", "nuvem": True}


def agendamento(ident: str) -> dict | None:
    """Estado da tarefa no Agendador, ou None se não existir."""
    if sys.platform != "win32":
        return _agendamento_nuvem()
    r = subprocess.run(["schtasks", "/Query", "/TN", nome_tarefa(ident), "/FO", "CSV", "/V"], capture_output=True,
                       text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        return None
    linhas = [l for l in r.stdout.splitlines() if l.strip()]
    if len(linhas) < 2:
        return {}
    import csv
    cab, val = next(csv.reader([linhas[0]])), next(csv.reader([linhas[1]]))
    info = dict(zip(cab, val))
    # os nomes das colunas dependem da língua do Windows: procura por palavras-chave
    def campo(*chaves):
        return next((v for k, v in info.items() if any(c in k.lower() for c in chaves)), "")
    return {"proxima": campo("next run", "próxima", "proxima"), "ultima": campo("last run", "última", "ultima"),
            "resultado": campo("last result", "último resultado", "ultimo resultado")}


# ---------------------------------------------------------------------------
# Linha de comandos (é o que o Agendador chama)
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Correr pesquisas automáticas guardadas.")
    ap.add_argument("--pesquisa", help="identificador da pesquisa (nome do ficheiro em pesquisas/, sem .json)")
    ap.add_argument("--todas", action="store_true", help="correr todas as pesquisas guardadas")
    ap.add_argument("--sem-notificacao", action="store_true")
    args = ap.parse_args()
    alvos = [p["id"] for p in listar()] if args.todas else [args.pesquisa] if args.pesquisa else []
    if not alvos:
        print("Indica --pesquisa <id> ou --todas. Pesquisas guardadas:", ", ".join(p["id"] for p in listar()) or "nenhuma")
        return 1
    registo = PASTA_AUTOMATICAS / "registo.log"
    PASTA_AUTOMATICAS.mkdir(parents=True, exist_ok=True)
    codigo = 0
    for ident in alvos:
        try:
            res = correr(ident, notificar=not args.sem_notificacao)
            linha = (f"{datetime.now():%Y-%m-%d %H:%M} {ident}: {res['passaram']} passam, {res['em_alerta']} em alerta, "
                     f"{len(res['novas'])} novas {res['novas']}")
        except Exception:  # noqa: BLE001 — sem janela aberta, o erro tem de ficar registado
            linha = f"{datetime.now():%Y-%m-%d %H:%M} {ident}: ERRO\n{traceback.format_exc()}"
            codigo = 1
            notificar_windows("Descoberta de Ações: erro numa pesquisa automática", f"{ident}: ver registo.log")
        print(linha)
        with registo.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
