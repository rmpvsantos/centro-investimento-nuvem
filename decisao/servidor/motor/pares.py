"""Construção do grupo de comparação: os maiores concorrentes do setor (segundo o Yahoo)."""

from __future__ import annotations

from metricas import comparar_com_grupo, estatisticas_grupo, pontuar

N_ALVO = 5


def montar_grupo(ticker, candidatos_setor, analisar_simbolo, n_alvo=N_ALVO):
    """
    candidatos_setor: símbolos do setor por ordem de dimensão (do maior para o menor).
    analisar_simbolo(simbolo) -> resultado de metricas.analisar (deve lançar exceção se falhar).
    Recolhe as n_alvo maiores empresas do setor com dados (sem contar a analisada), saltando as que falham.
    """
    usados = {ticker}
    validos: list[dict] = []
    falhas: list[str] = []
    tentativas = 0
    for simbolo in candidatos_setor:
        if len(validos) >= n_alvo or tentativas >= n_alvo * 2:
            break
        if simbolo in usados:
            continue
        usados.add(simbolo)
        tentativas += 1
        try:
            validos.append(analisar_simbolo(simbolo))
        except Exception as e:  # noqa: BLE001
            falhas.append(f"{simbolo}: {e}")

    return {
        "origem": "setor",
        "pares": [a["ticker"] for a in validos],
        "analises": validos,
        "estatisticas": estatisticas_grupo(validos) if validos else {},
        "falhas": falhas,
    }


MAX_MANUAL = 10


def normalizar_lista(texto: str, ticker: str = "") -> list[str]:
    """Tickers escritos à mão (separados por vírgula, espaço ou linha), sem repetidos nem o próprio ticker."""
    bruto = texto.replace(";", ",").replace("\n", ",").replace(" ", ",").split(",")
    out: list[str] = []
    for t in bruto:
        t = t.strip().upper()
        if t and t != ticker.strip().upper() and t not in out:
            out.append(t)
    return out


def montar_grupo_manual(ticker, simbolos, analisar_simbolo):
    """Grupo escolhido pelo utilizador: analisa exatamente estes símbolos (as falhas não são substituídas)."""
    validos: list[dict] = []
    falhas: list[str] = []
    for simbolo in normalizar_lista(",".join(simbolos), ticker)[:MAX_MANUAL]:
        try:
            validos.append(analisar_simbolo(simbolo))
        except Exception as e:  # noqa: BLE001
            falhas.append(f"{simbolo}: {e}")
    return {
        "origem": "manual",
        "pares": [a["ticker"] for a in validos],
        "analises": validos,
        "estatisticas": estatisticas_grupo(validos) if validos else {},
        "falhas": falhas,
    }


def aplicar_grupo(criterios, grupo, pe_cumpre=None, pe_parcial=None):
    """Devolve os critérios comparados com o grupo e a pontuação já recalculada. pe_cumpre/pe_parcial: ver
    metricas.comparar_com_grupo; se omitidos, usam-se os valores de origem desse módulo."""
    kw = {}
    if pe_cumpre is not None:
        kw["pe_cumpre"] = pe_cumpre
    if pe_parcial is not None:
        kw["pe_parcial"] = pe_parcial
    comparados = comparar_com_grupo(criterios, grupo["estatisticas"], **kw)
    score, score_max = pontuar(comparados)
    return comparados, score, score_max
