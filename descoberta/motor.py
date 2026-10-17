"""Junta as peças: dados → métricas → qualidade → avaliação → veredito."""

from __future__ import annotations

import math

import qualitativo
from avaliacao import Parametros, avaliar
from dados import obter_dados
from filtros import SETORES, TIPOS_MOAT
from metricas import analisar, cagr
from moat import analisar_moat
from qualidade import avaliar_qualidade
from sintese import ORDEM, veredito

SEM_JUROS = 999.0  # cobertura de juros quando a empresa não tem despesa de juros


def analisar_ticker(ticker: str, p: Parametros | None = None, usar_cache: bool = True, dados: dict | None = None) -> dict:
    p = p or Parametros()
    res = analisar(dados if dados is not None else obter_dados(ticker, usar_cache))
    qual = avaliar_qualidade(res)
    val = avaliar(res, p)
    mo = analisar_moat(res, val.get("taxa_desconto", p.taxa_desconto))
    registo = qualitativo.carregar(res["ticker"])
    pq = qualitativo.pontuacao(registo)
    return {"res": res, "qual": qual, "val": val, "moat": mo, "ver": veredito(qual, val, pq),
            "qualitativo": registo, "pont_qualitativa": pq}


def _num(v):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def linha_resumo(a: dict) -> dict:
    """Uma linha por empresa, com valores numéricos (para ordenar, filtrar e exportar)."""
    res, qual, val, ver = a["res"], a["qual"], a["val"], a["ver"]
    tab = res["anual"]
    financeira = res.get("modelo_financeiro")
    cen = {c["cenario"]: c.get("valor") for c in val.get("cenarios", [])}
    mcap, fcf = res.get("capitalizacao_fin"), res["atual"].get("fcf_ajustado")
    gi, gb = _num(val.get("crescimento_implicito")), _num(val.get("crescimento_base"))

    acoes = tab["acoes"].dropna()
    diluicao = None
    if len(acoes) >= 2 and acoes.iloc[0] > 0:
        anos = max((acoes.index[-1] - acoes.index[0]).days / 365.25, 1)
        diluicao = _num((acoes.iloc[-1] / acoes.iloc[0]) ** (1 / anos) - 1)
    fcf_rep = res["atual"].get("fcf")  # FCF reportado dos últimos 12 meses (o P/FCF usual)
    rec = tab["recompras"].dropna()
    recompras = float(rec.iloc[-1]) if len(rec) else 0.0  # sem linha de recompras no Yahoo = não recomprou
    cob = res.get("cobertura_atual")
    cob = SEM_JUROS if isinstance(cob, float) and math.isinf(cob) else _num(cob)

    return {
        "Ticker": res["ticker"],
        "Empresa": res["nome"],
        "País": res.get("pais"),
        "Setor": SETORES.get(res.get("setor"), res.get("setor")),
        "Indústria": res.get("industria"),
        "Classificação": ver["titulo"],
        "ordem": ORDEM.index(ver["classe"]),
        "Qualidade": _num(qual.get("pontuacao")),
        "Preço (nota)": _num(val.get("pontuacao")),
        "Qualitativa": _num(a.get("pont_qualitativa")),
        "Moat": a["moat"]["titulo"],
        "Moat (nota)": _num(a["moat"]["pontuacao"]),
        "Tendência do moat": a["moat"]["tendencia_titulo"],
        # tipos de moat: fontes com nota forte (≥ 70), só quando há moat (largo ou estreito). Uma margem bruta
        # alta sem retornos acima do custo de capital não é vantagem competitiva. Separados por "; " para o filtro.
        "Tipos de moat": "; ".join(TIPOS_MOAT[f["fonte"]] for f in a["moat"]["fontes"]
                                   if f["fonte"] in TIPOS_MOAT and (f["pontuacao"] or 0) >= 70)
                         if a["moat"]["classe"] in ("largo", "estreito") else "",
        "Confiança": _num(ver.get("confianca")),
        "Alertas graves": sum(1 for x in qual["alertas"] if x["gravidade"] == "grave"),
        "Moeda": res.get("moeda_cotacao"),
        "Preço atual": _num(res.get("preco_atual")),
        "Valor esperado": _num(val.get("valor_esperado")),
        "Desconto": _num(val.get("desconto")),
        "Preço de entrada": _num(val.get("preco_entrada")),
        "Valor pessimista": _num(cen.get("Pessimista")),
        "Valor otimista": _num(cen.get("Otimista")),
        "Crescimento implícito": gi,
        "Crescimento base": gb,
        "Crescimento exigido vs base": gi - gb if gi is not None and gb is not None else None,
        "ROIC mediano": None if financeira else _num(tab["roic"].median()),
        "ROE mediano": _num(tab["roe"].median()),
        "Margem bruta mediana": _num(tab["margem_bruta"].median()),
        "Margem FCF mediana": None if financeira else _num(tab["margem_fcf_ajustado"].median()),
        "Crescimento receitas (CAGR)": _num(cagr(tab["receita"])),
        "Diluição anual": diluicao,
        "Rendibilidade FCF": None if financeira or _num(fcf) is None or not _num(mcap) else _num(fcf / mcap),
        "P/FCF": (_num(mcap / fcf_rep) if not financeira and _num(mcap) and _num(fcf_rep) and fcf_rep > 0 else None),
        "Recompras (% capitalização)": (_num(recompras / mcap) if _num(mcap) and _num(recompras) is not None
                                        else None),
        "EV/EBIT": _num(res.get("ev_ebit_atual")),
        "P/E": _num(res.get("pe_atual")),
        "Dívida líquida/EBITDA": None if financeira else _num(tab["divida_liq_ebitda"].iloc[-1]),
        "Cobertura de juros": None if financeira else cob,
        "Piotroski": None if financeira else _num(qual["piotroski"].get("valor")),
        "Altman Z": _num(qual["altman"].get("valor")),
        "Capitalização (mil milhões)": _num(mcap / 1e9) if _num(mcap) else None,
        "Moeda das contas": res.get("moeda"),
    }
