"""
Calendário de resultados (sem rede: recebe o que o Yahoo devolve em Ticker.calendar e Ticker.get_earnings_dates).

  - próximos resultados: data, momento (antes da abertura / depois do fecho), estimativas de EPS e de receita;
  - histórico: EPS estimado, EPS reportado e surpresa dos últimos trimestres;
  - dividendos: data ex-dividendo e de pagamento;
  - relatório por pedir: os resultados mais recentes saíram depois do teu último relatório sobre a empresa.
"""
from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and not math.isinf(v)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if _ok(f) else None


def momento(ts: pd.Timestamp) -> str | None:
    """Hora da bolsa -> "antes da abertura" / "depois do fecho". Horas a meio do dia ou 00:00 são, quase sempre, horas
    genéricas que o Yahoo usa enquanto a empresa não confirma: nesse caso não se diz nada."""
    h = ts.hour + ts.minute / 60
    if h == 0:
        return None
    return "antes da abertura" if h < 9.5 else "depois do fecho" if h >= 16 else None


def montar(cal: dict | None, datas: pd.DataFrame | None, hoje: date | None = None) -> dict:
    hoje = hoje or date.today()
    cal = cal or {}
    futuros, historico = [], []
    if datas is not None and not datas.empty:
        for ts, linha in datas.sort_index().iterrows():
            ts = pd.Timestamp(ts)
            item = {"data": ts.date().isoformat(), "momento": momento(ts),
                    "eps_estimado": _num(linha.get("EPS Estimate")), "eps": _num(linha.get("Reported EPS")),
                    "surpresa": (_num(linha.get("Surprise(%)")) or 0) / 100 if _num(linha.get("Surprise(%)")) is not None else None}
            (futuros if ts.date() >= hoje and item["eps"] is None else historico).append(item)

    # O calendário do Yahoo às vezes tem a data seguinte quando a lista ainda não a tem (e vice-versa)
    datas_cal = [d for d in (cal.get("Earnings Date") or []) if isinstance(d, date) and d >= hoje]
    proximo = futuros[0] if futuros else ({"data": min(datas_cal).isoformat(), "momento": None, "eps_estimado": None} if datas_cal else None)
    if proximo:
        if proximo.get("eps_estimado") is None:
            proximo["eps_estimado"] = _num(cal.get("Earnings Average"))
        if datas_cal and min(datas_cal).isoformat() == proximo["data"]:
            proximo["receita_estimada"] = _num(cal.get("Revenue Average"))
            proximo["eps_intervalo"] = [_num(cal.get("Earnings Low")), _num(cal.get("Earnings High"))]
        proximo["dias"] = (date.fromisoformat(proximo["data"]) - hoje).days

    reportados = [h for h in historico if h["eps"] is not None]
    ultimo = reportados[-1] if reportados else None
    # Às vezes o historial do Yahoo está atrasado (ex.: ADR) mas o calendário tem a data dos últimos resultados
    passadas = [d for d in (cal.get("Earnings Date") or []) if isinstance(d, date) and d < hoje]
    if passadas and (not ultimo or max(passadas).isoformat() > ultimo["data"]):
        ultimo = {"data": max(passadas).isoformat(), "momento": None, "eps_estimado": None, "eps": None, "surpresa": None}
    surpresas = [h["surpresa"] for h in reportados[-8:] if h["surpresa"] is not None]
    fmt = lambda d: d.isoformat() if isinstance(d, date) else None  # noqa: E731
    return {
        "proximo": proximo,
        "ultimo": ultimo,
        "historico": list(reversed(reportados[-8:])),
        "batidas": {"n": len(surpresas), "acima": sum(1 for s in surpresas if s > 0)} if surpresas else None,
        "dividendo": {"ex": fmt(cal.get("Ex-Dividend Date")), "pagamento": fmt(cal.get("Dividend Date"))},
    }


def estado_relatorio(ultimo_resultado: str | None, data_ultimo_relatorio: str | None, tem_relatorios: bool) -> dict:
    """Os resultados mais recentes já têm relatório teu? (compara as datas: o relatório tem de ser posterior)."""
    if not tem_relatorios:
        return {"estado": "sem_relatorios", "texto": "sem relatórios teus sobre esta empresa"}
    if not ultimo_resultado:
        return {"estado": "desconhecido", "texto": "sem data dos últimos resultados"}
    if data_ultimo_relatorio and data_ultimo_relatorio >= ultimo_resultado:
        return {"estado": "em_dia", "texto": f"relatório em dia (resultados de {ultimo_resultado})"}
    return {"estado": "por_pedir", "texto": f"resultados de {ultimo_resultado} ainda sem relatório teu"}
