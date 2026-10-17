"""Obtenção de dados do Yahoo Finance (gratuito, sem chave) através do yfinance, com cache em disco."""

from __future__ import annotations

import pickle
import time
from pathlib import Path

from metricas import moeda_principal

PASTA_CACHE = Path(__file__).parent / ".cache"
VALIDADE_HORAS = 24


def _ficheiro_cache(chave: str) -> Path:
    seguro = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in chave)
    return PASTA_CACHE / f"{seguro}.pkl"


def _ler_cache(chave: str, validade_horas: float):
    f = _ficheiro_cache(chave)
    if not f.exists() or (time.time() - f.stat().st_mtime) > validade_horas * 3600:
        return None
    try:
        with f.open("rb") as fh:
            return pickle.load(fh)
    except Exception:  # noqa: BLE001 — cache corrompida: volta a pedir
        return None


def _gravar_cache(chave: str, valor) -> None:
    PASTA_CACHE.mkdir(exist_ok=True)
    with _ficheiro_cache(chave).open("wb") as fh:
        pickle.dump(valor, fh)


def com_repeticao(funcao, tentativas: int = 3, espera: float = 5.0):
    """O Yahoo devolve 'Too Many Requests' quando se pede demasiado depressa: espera e tenta outra vez."""
    for i in range(tentativas):
        try:
            return funcao()
        except Exception as e:  # noqa: BLE001
            limite = "Too Many Requests" in str(e) or "Rate" in type(e).__name__
            if not limite or i == tentativas - 1:
                raise
            time.sleep(espera * (i + 1) ** 2)
    return None


def serie_cambio(de: str, para: str):
    """Unidades de `para` por 1 unidade de `de`, diária, últimos 7 anos."""
    import yfinance as yf

    chave = f"fx_{de}{para}"
    em_cache = _ler_cache(chave, VALIDADE_HORAS)
    if em_cache is not None:
        return em_cache
    hist = com_repeticao(lambda: yf.Ticker(f"{de}{para}=X").history(period="7y"))
    serie = hist["Close"] if hist is not None and not hist.empty else None
    if serie is not None:
        _gravar_cache(chave, serie)
    return serie


def _crescimento_analistas(t) -> dict:
    """Crescimento de receitas esperado pelos analistas: exercício corrente (0y) e seguinte (+1y)."""
    try:
        est = t.revenue_estimate
    except Exception:  # noqa: BLE001
        return {}
    if est is None or getattr(est, "empty", True) or "growth" not in est.columns:
        return {}
    out = {}
    for periodo in ["0y", "+1y"]:
        if periodo in est.index:
            g = est.loc[periodo, "growth"]
            n = est.loc[periodo, "numberOfAnalysts"] if "numberOfAnalysts" in est.columns else None
            if g is not None and g == g:
                out[periodo] = {"crescimento": float(g), "analistas": int(n) if n == n and n is not None else None}
    return out


def obter_dados(ticker: str, usar_cache: bool = True) -> dict:
    import yfinance as yf  # importado aqui para o motor de cálculo poder ser testado sem rede

    ticker = ticker.strip().upper()
    if usar_cache:
        em_cache = _ler_cache(f"dados_{ticker}", VALIDADE_HORAS)
        if em_cache is not None:
            return em_cache

    t = yf.Ticker(ticker)

    try:
        info = com_repeticao(lambda: t.info) or {}
    except Exception:  # noqa: BLE001
        info = {}

    income = com_repeticao(lambda: t.income_stmt)
    if income is None or income.empty:
        raise ValueError(
            f"Sem dados para '{ticker}'. Confirma o ticker (europeus levam sufixo da bolsa: "
            "ASML.AS, SAP.DE, MC.PA, NOVO-B.CO, SHEL.L) ou tenta novamente dentro de alguns minutos "
            "(o Yahoo limita pedidos em excesso)."
        )

    precos = com_repeticao(lambda: t.history(period="7y", auto_adjust=False))
    precos = precos["Close"] if precos is not None and not precos.empty else None

    try:
        splits = t.splits
    except Exception:  # noqa: BLE001
        splits = None

    moeda_cot, _ = moeda_principal(info.get("currency"))
    moeda_fin = info.get("financialCurrency")
    fx = None
    if moeda_cot and moeda_fin and moeda_cot != moeda_fin:
        try:
            fx = serie_cambio(moeda_fin, moeda_cot)
        except Exception:  # noqa: BLE001
            fx = None

    dados = {
        "ticker": ticker,
        "info": info,
        "income": income,
        "balance": com_repeticao(lambda: t.balance_sheet),
        "cashflow": com_repeticao(lambda: t.cashflow),
        "income_q": com_repeticao(lambda: t.quarterly_income_stmt),
        "cashflow_q": com_repeticao(lambda: t.quarterly_cashflow),
        "balance_q": com_repeticao(lambda: t.quarterly_balance_sheet),
        "precos": precos,
        "splits": splits,
        "fx": fx,
        "crescimento_analistas": _crescimento_analistas(t),
    }
    _gravar_cache(f"dados_{ticker}", dados)
    return dados
