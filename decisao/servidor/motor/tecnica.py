"""
Indicadores técnicos (sem Streamlit), calculados sempre a partir dos preços diários brutos: médias móveis simples, RSI de
Wilder, MACD e suportes/resistências por pontos de viragem. Cada função devolve uma lista alinhada com a entrada, com NaN
onde ainda não há dados suficientes.
"""
from __future__ import annotations

import math

NAN = float("nan")


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and v == v and not math.isinf(v)


def mm(valores: list[float], periodo: int) -> list[float]:
    """Média móvel simples."""
    out, soma = [NAN] * len(valores), 0.0
    for i, v in enumerate(valores):
        soma += v
        if i >= periodo:
            soma -= valores[i - periodo]
        if i >= periodo - 1:
            out[i] = soma / periodo
    return out


def mme(valores: list[float], periodo: int) -> list[float]:
    """Média móvel exponencial, semeada com a média simples do primeiro período; ignora os NaN iniciais."""
    out = [NAN] * len(valores)
    k = 2 / (periodo + 1)
    inicio = next((i for i, v in enumerate(valores) if _ok(v)), None)
    if inicio is None or len(valores) - inicio < periodo:
        return out
    ant = sum(valores[inicio:inicio + periodo]) / periodo
    out[inicio + periodo - 1] = ant
    for i in range(inicio + periodo, len(valores)):
        ant = valores[i] * k + ant * (1 - k)
        out[i] = ant
    return out


def rsi(valores: list[float], periodo: int = 14) -> list[float]:
    """RSI de Wilder (médias de ganhos e perdas suavizadas com 1/período)."""
    out = [NAN] * len(valores)
    if len(valores) <= periodo:
        return out
    difs = [valores[i] - valores[i - 1] for i in range(1, periodo + 1)]
    ganho = sum(d for d in difs if d > 0) / periodo
    perda = sum(-d for d in difs if d < 0) / periodo

    def valor():
        return 100.0 if perda == 0 else 100 - 100 / (1 + ganho / perda)

    out[periodo] = valor()
    for i in range(periodo + 1, len(valores)):
        d = valores[i] - valores[i - 1]
        ganho = (ganho * (periodo - 1) + max(d, 0)) / periodo
        perda = (perda * (periodo - 1) + max(-d, 0)) / periodo
        out[i] = valor()
    return out


def macd(valores: list[float], rapida: int = 12, lenta: int = 26, sinal: int = 9) -> dict:
    r, l = mme(valores, rapida), mme(valores, lenta)
    linha = [a - b if _ok(a) and _ok(b) else NAN for a, b in zip(r, l)]
    sin = mme(linha, sinal)
    hist = [a - b if _ok(a) and _ok(b) else NAN for a, b in zip(linha, sin)]
    return {"linha": linha, "sinal": sin, "histograma": hist}


def suportes_resistencias(maximos: list[float], minimos: list[float], preco: float, sessoes: int = 250,
                          janela: int = 5, tolerancia: float = 0.015, maximo: int = 3) -> dict:
    """
    Pontos de viragem das últimas `sessoes`: um máximo (mínimo) é o mais alto (baixo) de ±`janela` sessões. Pontos a menos de
    `tolerancia` uns dos outros juntam-se num nível; só contam níveis com pelo menos 2 toques. Devolve até `maximo` suportes
    (abaixo do preço) e resistências (acima), os mais tocados primeiro e, em empate, os mais próximos do preço.
    """
    hi, lo = maximos[-sessoes:], minimos[-sessoes:]
    pontos = []
    for i in range(janela, len(hi) - janela):
        if hi[i] == max(hi[i - janela:i + janela + 1]):
            pontos.append(hi[i])
        if lo[i] == min(lo[i - janela:i + janela + 1]):
            pontos.append(lo[i])

    grupos: list[list[float]] = []
    for p in sorted(pontos):
        if grupos and (p - grupos[-1][0]) / grupos[-1][0] <= tolerancia:
            grupos[-1].append(p)
        else:
            grupos.append([p])
    niveis = [{"preco": sum(g) / len(g), "toques": len(g)} for g in grupos if len(g) >= 2]

    def ordem(n):
        return (-n["toques"], abs(n["preco"] - preco))

    sup = sorted([n for n in niveis if n["preco"] < preco], key=ordem)[:maximo]
    res = sorted([n for n in niveis if n["preco"] >= preco], key=ordem)[:maximo]
    return {"suportes": sorted(sup, key=lambda n: -n["preco"]), "resistencias": sorted(res, key=lambda n: n["preco"])}


def cruzamento_recente(linha: list[float], sinal: list[float], sessoes: int = 10) -> int | None:
    """Há quantas sessões a linha MACD cruzou a de sinal (0 = na última sessão); None se não cruzou nas últimas `sessoes`."""
    n = len(linha) - 1
    for i in range(n, max(n - sessoes, 0), -1):
        a, b, c, d = linha[i], sinal[i], linha[i - 1], sinal[i - 1]
        if not all(_ok(x) for x in (a, b, c, d)):
            return None
        if (a - b > 0) != (c - d > 0):
            return n - i
    return None


def resumo(fechos: list[float], maximos: list[float], minimos: list[float]) -> dict:
    """Todos os indicadores e a leitura da última sessão."""
    m20, m50, m200 = mm(fechos, 20), mm(fechos, 50), mm(fechos, 200)
    r = rsi(fechos)
    md = macd(fechos)
    preco = fechos[-1]
    return {
        "preco": preco,
        "mm20": m20, "mm50": m50, "mm200": m200, "rsi": r, "macd": md,
        "ultimo": {
            "mm50": m50[-1], "mm200": m200[-1], "rsi": r[-1],
            "macd": md["linha"][-1], "macd_sinal": md["sinal"][-1],
            "cruzamento_macd": cruzamento_recente(md["linha"], md["sinal"]),
        },
        "niveis": suportes_resistencias(maximos, minimos, preco),
    }


def tendencia(ultimo: dict, preco: float) -> str:
    """"subida": preço acima da MM200 e MM50 acima da MM200; "descida": o inverso; "lateral": o resto; "nd": sem MM200."""
    m50, m200 = ultimo["mm50"], ultimo["mm200"]
    if not (_ok(m50) and _ok(m200)):
        return "nd"
    if preco > m200 and m50 > m200:
        return "subida"
    if preco < m200 and m50 < m200:
        return "descida"
    return "lateral"
