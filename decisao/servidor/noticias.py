"""
Notícias por empresa (Yahoo Finance), traduzidas para pt-PT e arquivadas na wiki: noticias/<TICKER>.json na pasta de
dados. Cada recolha acrescenta as notícias novas (nunca apaga as antigas), para ficar um historial.

Relevância: só entram notícias que o Yahoo associa ao ticker da empresa ou que a mencionam no título ou no resumo
(o Yahoo mistura notícias gerais do setor na lista de cada empresa).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

import config
import traducao
import wiki

PASTA = "noticias"
INTERVALO_HORAS = 6
_cadeados: dict[str, threading.Lock] = {}
_cadeado_global = threading.Lock()

PALAVRAS_GENERICAS = {"inc", "corp", "corporation", "co", "company", "holding", "holdings", "group", "plc", "sa", "nv", "n.v.", "ag", "ltd",
                      "the", "and", "&", "de", "class", "a", "b"}


def _ficheiro(ticker: str) -> str:
    return f"{PASTA}/{wiki.seguro(ticker)}.json"


def ler(ticker: str) -> dict:
    return config.ler_json(_ficheiro(ticker), {"atualizado": None, "noticias": {}})


def palavras_nome(nome: str | None) -> list[str]:
    """Palavras distintivas do nome ("Microsoft Corporation" -> ["microsoft"])."""
    partes = re.findall(r"[a-z0-9][a-z0-9'\-]+", (nome or "").lower())
    return [p for p in partes if p not in PALAVRAS_GENERICAS and len(p) >= 3][:2]


def relevante(item: dict, ticker: str, nome: str | None) -> bool:
    base = ticker.split(".")[0].upper()
    if base in {s.split(".")[0].upper() for s in item.get("tickers", [])}:
        return True
    texto = f"{item.get('titulo_original', '')} {item.get('resumo_original', '')}".lower()
    if re.search(rf"\b{re.escape(base.lower())}\b", texto) and len(base) >= 3:
        return True
    return any(re.search(rf"\b{re.escape(p)}\b", texto) for p in palavras_nome(nome))


def normalizar(bruto: dict) -> dict | None:
    c = bruto.get("content", bruto)
    titulo = c.get("title")
    if not titulo:
        return None
    url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
    tickers = [s.get("symbol") for s in ((c.get("finance") or {}).get("stockTickers") or []) if s.get("symbol")]
    return {
        "id": c.get("id") or bruto.get("id") or url or titulo,
        "data": c.get("pubDate") or c.get("displayTime"),
        "titulo_original": titulo,
        "resumo_original": c.get("summary") or c.get("description") or "",
        "fonte": (c.get("provider") or {}).get("displayName"),
        "url": url,
        "tipo": c.get("contentType"),
        "tickers": tickers,
    }


def recolher(ticker: str, nome: str | None = None, forcar: bool = False) -> dict:
    """Vai buscar as notícias ao Yahoo (se a última recolha tiver mais de INTERVALO_HORAS), traduz as novas e guarda."""
    import yfinance as yf

    t = ticker.upper()
    with _cadeado_global:
        cad = _cadeados.setdefault(t, threading.Lock())
    with cad:
        arquivo = ler(t)
        if not forcar and arquivo.get("atualizado"):
            idade = time.time() - datetime.fromisoformat(arquivo["atualizado"]).timestamp()
            if idade < INTERVALO_HORAS * 3600:
                return arquivo
        brutas = yf.Ticker(t).news or []
        novas = 0
        for b in brutas:
            n = normalizar(b)
            if not n or n["id"] in arquivo["noticias"] or not relevante(n, t, nome):
                continue
            n["titulo"] = traducao.traduzir(n["titulo_original"]) or n["titulo_original"]
            n["resumo"] = traducao.traduzir(n["resumo_original"]) or n["resumo_original"] if n["resumo_original"] else ""
            n["traduzido"] = n["titulo"] != n["titulo_original"]
            n["recolhida"] = datetime.now().isoformat(timespec="seconds")
            arquivo["noticias"][n["id"]] = n
            novas += 1
        arquivo["atualizado"] = datetime.now().isoformat(timespec="seconds")
        arquivo["novas_ultima_recolha"] = novas
        config.escrever_json(_ficheiro(t), arquivo)
        return arquivo


def lista(ticker: str, limite: int | None = None) -> list[dict]:
    """Notícias guardadas, das mais recentes para as mais antigas."""
    ns = sorted(ler(ticker)["noticias"].values(), key=lambda n: n.get("data") or "", reverse=True)
    return ns[:limite] if limite else ns


def de_hoje(tickers: list[str], dias: int = 1) -> list[dict]:
    """Notícias publicadas nas últimas `dias` × 24 horas para um conjunto de empresas."""
    agora = datetime.now(timezone.utc)
    saida = []
    for t in tickers:
        for n in lista(t):
            try:
                d = datetime.fromisoformat(str(n.get("data")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (agora - d).total_seconds() <= dias * 86400:
                saida.append({**n, "ticker": t})
    return sorted(saida, key=lambda n: n.get("data") or "", reverse=True)


def recolha_periodica(obter_seguidas, obter_nome, parar: threading.Event) -> None:
    """Corre num fio à parte enquanto a app está aberta: recolhe as notícias das empresas que acompanhas."""
    while not parar.is_set():
        for t in obter_seguidas():
            if parar.is_set():
                break
            try:
                recolher(t, obter_nome(t))
            except Exception:  # noqa: BLE001  (uma empresa sem notícias ou sem rede não pode parar as outras)
                pass
        parar.wait(3600)
