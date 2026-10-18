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
        **_indicadores_pesquisa(res, a, financeira),
    }


def _mediana(s) -> float | None:
    s = s.replace([math.inf, -math.inf], float("nan")).dropna()
    return _num(s.median()) if len(s) else None


def _ultimo(s) -> float | None:
    s = s.dropna()
    return _num(s.iloc[-1]) if len(s) else None


def _div(a, b, so_positivo_b: bool = True) -> float | None:
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0 or (so_positivo_b and b < 0):
        return None
    return _num(a / b)


def _indicadores_pesquisa(res: dict, a: dict, financeira: bool) -> dict:
    """Os critérios da Pesquisa (pedido do Rui a 18/10/2026) que o resumo ainda não tinha. Rácios de balanço e de
    resultados com os exercícios anuais (medianas onde a série tem vários anos) e os valores atuais; os da avaliação
    com a capitalização atual. Nas financeiras (bancos, seguradoras) o que não se aplica fica vazio."""
    tab, at, ind = res["anual"], res["atual"], res.get("indicadores_yahoo") or {}
    mcap, nd = res.get("capitalizacao_fin"), at.get("divida_liquida")
    ebitda_at, fcf_at, cp = at.get("ebitda"), at.get("fcf"), at.get("capital_proprio")
    capital_empregue = tab["ativo_total"] - tab["passivo_corrente"]
    div_ult, luc_ult = _ultimo(tab["dividendos"]), _ultimo(tab["lucro_liquido"])
    nivel_moat = {"largo": 2, "estreito": 1, "sem": 0}.get(a["moat"].get("classe"))
    return {
        "Crescimento EPS (CAGR)": _num(cagr(tab["eps"])),
        "Crescimento FCF (CAGR)": None if financeira else _num(cagr(tab["fcf"])),
        "Crescimento EBITDA (CAGR)": None if financeira else _num(cagr(tab["ebitda"])),
        "Capex (% receitas)": None if financeira else _mediana(tab["capex_receita"]),
        "Margem líquida mediana": _mediana(tab["lucro_liquido"] / tab["receita"]),
        "Margem EBITDA mediana": None if financeira else _mediana(tab["margem_ebitda"]),
        "Rotação do ativo": _mediana(tab["receita"] / tab["ativo_total"]),
        "ROA mediano": _mediana(tab["lucro_liquido"] / tab["ativo_total"]),
        "ROCE mediano": None if financeira else _mediana(tab["ebit"] / capital_empregue.where(capital_empregue > 0)),
        "Caixa líquida (% capitalização)": None if financeira else _div(-nd if _num(nd) is not None else None, mcap),
        "Dívida líquida/Capital próprio": None if financeira else _div(nd, cp),
        "Dívida líquida/FCF": None if financeira else _div(nd, fcf_at),
        "Liquidez corrente": None if financeira else _ultimo(tab["ativo_corrente"] / tab["passivo_corrente"]),
        "Quick ratio": None if financeira else _num(ind.get("quick_ratio")),
        "P/E futuro": _num(ind.get("pe_futuro")),
        "PEG": _num(ind.get("peg")),
        "P/B": _num(ind.get("p_b")),
        "EV/EBITDA": None if financeira else _div(res.get("ev_atual"), ebitda_at),
        "Payout": _div(div_ult, luc_ult),
        "SBC (% receitas)": _mediana(tab["sbc_receita"]),
        "Dividend yield": _div(div_ult, mcap) if div_ult is not None else 0.0,
        "Moat (nível)": nivel_moat,
    }
