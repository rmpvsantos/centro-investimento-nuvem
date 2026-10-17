"""
Qualidade do negócio: é um bom negócio, independentemente do preço?

Recebe o resultado de metricas.analisar e devolve:
- uma pontuação de 0 a 100, decomposta por componente (para se ver de onde vem);
- o Piotroski F-Score e o Altman Z-Score;
- os alertas de "armadilha de valor" (graves e de atenção).

Cada componente sem dados sai da pontuação em vez de contar como zero; a
"confiança" indica que percentagem da pontuação máxima foi possível avaliar.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from metricas import cagr, tendencia


def _mediana(s: pd.Series) -> float:
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    return float(s.median()) if len(s) else float("nan")


def _escala(valor: float, degraus: list[tuple[float, float]], maior_e_melhor: bool = True) -> float:
    """Devolve os pontos do primeiro degrau atingido. degraus = [(limiar, pontos), ...] do melhor para o pior."""
    for limiar, pontos in degraus:
        if (valor >= limiar) if maior_e_melhor else (valor <= limiar):
            return pontos
    return 0.0


# ---------------------------------------------------------------------------
# Piotroski F-Score (9 sinais binários, último exercício face ao anterior)
# ---------------------------------------------------------------------------
def piotroski(tab: pd.DataFrame) -> dict:
    if len(tab) < 2:
        return {"valor": None, "sinais": [], "avaliados": 0}
    a, b = tab.iloc[-1], tab.iloc[-2]

    def rac(x, y):
        return x / y if (not pd.isna(x) and not pd.isna(y) and y != 0) else np.nan

    sinais = []

    def sinal(nome, cond):
        if cond is None or (isinstance(cond, float) and math.isnan(cond)):
            return
        sinais.append((nome, bool(cond)))

    roa_a, roa_b = rac(a["lucro_liquido"], a["ativo_total"]), rac(b["lucro_liquido"], b["ativo_total"])
    sinal("Resultado líquido positivo", a["lucro_liquido"] > 0 if not pd.isna(a["lucro_liquido"]) else None)
    sinal("Fluxo de caixa operacional positivo", a["cfo"] > 0 if not pd.isna(a["cfo"]) else None)
    sinal("ROA a subir", roa_a > roa_b if not (pd.isna(roa_a) or pd.isna(roa_b)) else None)
    sinal("Caixa operacional acima do resultado", a["cfo"] > a["lucro_liquido"]
          if not (pd.isna(a["cfo"]) or pd.isna(a["lucro_liquido"])) else None)
    alav_a, alav_b = rac(a["divida_lp"], a["ativo_total"]), rac(b["divida_lp"], b["ativo_total"])
    sinal("Endividamento de longo prazo a descer", alav_a <= alav_b if not (pd.isna(alav_a) or pd.isna(alav_b)) else None)
    liq_a, liq_b = rac(a["ativo_corrente"], a["passivo_corrente"]), rac(b["ativo_corrente"], b["passivo_corrente"])
    sinal("Liquidez corrente a subir", liq_a > liq_b if not (pd.isna(liq_a) or pd.isna(liq_b)) else None)
    sinal("Sem emissão de ações", a["acoes"] <= b["acoes"] * 1.005 if not (pd.isna(a["acoes"]) or pd.isna(b["acoes"])) else None)
    sinal("Margem bruta a subir", a["margem_bruta"] > b["margem_bruta"]
          if not (pd.isna(a["margem_bruta"]) or pd.isna(b["margem_bruta"])) else None)
    rot_a, rot_b = rac(a["receita"], a["ativo_total"]), rac(b["receita"], b["ativo_total"])
    sinal("Rotação do ativo a subir", rot_a > rot_b if not (pd.isna(rot_a) or pd.isna(rot_b)) else None)

    if len(sinais) < 7:
        return {"valor": None, "sinais": sinais, "avaliados": len(sinais)}
    # normaliza para a escala de 9 quando falta um ou dois sinais
    valor = sum(ok for _, ok in sinais) * 9 / len(sinais)
    return {"valor": round(valor, 1), "sinais": sinais, "avaliados": len(sinais)}


# ---------------------------------------------------------------------------
# Altman Z-Score (versão original, com valor de mercado do capital próprio)
# ---------------------------------------------------------------------------
def altman(res: dict) -> dict:
    tab = res["anual"]
    a = tab.iloc[-1]
    ta, tl = a["ativo_total"], a["passivo_total"]
    mcap = res.get("capitalizacao_fin")
    campos = [ta, tl, a["ativo_corrente"], a["passivo_corrente"], a["resultados_transitados"], a["ebit"], a["receita"]]
    if res.get("modelo_financeiro") or any(pd.isna(x) for x in campos) or not mcap or ta <= 0 or tl <= 0:
        return {"valor": None, "zona": "n/d"}
    z = (1.2 * (a["ativo_corrente"] - a["passivo_corrente"]) / ta + 1.4 * a["resultados_transitados"] / ta
         + 3.3 * a["ebit"] / ta + 0.6 * mcap / tl + 1.0 * a["receita"] / ta)
    zona = "segura" if z >= 2.99 else "cinzenta" if z >= 1.81 else "perigo"
    return {"valor": float(z), "zona": zona}


# ---------------------------------------------------------------------------
# Pontuação de qualidade
# ---------------------------------------------------------------------------
def _componentes_nao_financeira(res: dict, pio: dict) -> list[dict]:
    tab = res["anual"]
    comp = []

    def add(nome, pontos, maximo, detalhe):
        comp.append({"componente": nome, "pontos": pontos, "maximo": maximo, "detalhe": detalhe})

    roic = tab["roic"].replace([np.inf, -np.inf], np.nan)
    roic_med = _mediana(roic)
    capital_negativo = (tab["capital_investido_medio"] <= 0).iloc[-1] and (tab["ebit"] > 0).iloc[-1]
    if capital_negativo:
        add("Retorno sobre o capital investido (ROIC)", 25, 25,
            "capital investido negativo com EBIT positivo: o negócio financia-se com o dinheiro dos clientes")
        add("ROIC consistente", 5, 5, "idem")
    elif math.isnan(roic_med):
        add("Retorno sobre o capital investido (ROIC)", None, 25, "sem dados")
        add("ROIC consistente", None, 5, "sem dados")
    else:
        add("Retorno sobre o capital investido (ROIC)",
            _escala(roic_med, [(0.20, 25), (0.15, 20), (0.12, 15), (0.09, 8)]), 25,
            f"mediana {roic_med*100:.1f}% (último {roic.dropna().iloc[-1]*100:.1f}%)")
        roic_min = float(roic.dropna().min())
        add("ROIC consistente", _escala(roic_min, [(0.10, 5), (0.06, 2.5)]), 5, f"pior ano {roic_min*100:.1f}%")

    mb = tab["margem_bruta"].dropna()
    if len(mb) >= 2:
        nivel = _escala(float(mb.median()), [(0.50, 5), (0.30, 3), (0.0, 1)])
        desvio = float(mb.std())
        estab = _escala(desvio, [(0.02, 5), (0.05, 2.5)], maior_e_melhor=False)
        add("Margem bruta: nível e estabilidade", nivel + estab, 10,
            f"mediana {mb.median()*100:.1f}%, desvio-padrão {desvio*100:.1f} p.p. (estável = poder de fixação de preços)")
    else:
        add("Margem bruta: nível e estabilidade", None, 10, "sem margem bruta reportada")

    t = tendencia(tab["receita"], "subir")
    c = cagr(tab["receita"])
    if t["estado"] == "nd":
        add("Crescimento de receitas consistente", None, 10, t["detalhe"])
    else:
        pts = {"cumpre": 10, "parcial": 5, "falha": 0}[t["estado"]]
        if not math.isnan(c) and c < 0:
            pts = 0
        add("Crescimento de receitas consistente", pts, 10,
            t["detalhe"] + (f", CAGR {c*100:.1f}%" if not math.isnan(c) else ""))

    conv = _mediana(tab["conversao_fcf"])
    if math.isnan(conv):
        add("Conversão do lucro em caixa (FCF / lucro)", None, 10, "sem dados ou lucro negativo")
    else:
        add("Conversão do lucro em caixa (FCF / lucro)", _escala(conv, [(0.9, 10), (0.7, 6), (0.5, 3)]), 10,
            f"mediana {conv:.2f}x" + (": parte do lucro contabilístico não chega à caixa" if conv < 0.7 else ""))

    mf = _mediana(tab["margem_fcf_ajustado"])
    if math.isnan(mf):
        add("Margem de FCF (descontada a remuneração em ações)", None, 10, "sem dados")
    else:
        add("Margem de FCF (descontada a remuneração em ações)",
            _escala(mf, [(0.15, 10), (0.10, 7), (0.05, 4), (0.0001, 2)]), 10, f"mediana {mf*100:.1f}%")

    nd_ebitda = tab["divida_liq_ebitda"].iloc[-1]
    nd = tab["divida_liquida"].iloc[-1]
    cob = res["cobertura_atual"]
    if not pd.isna(nd) and nd <= 0:
        p_div, det_div = 10, "caixa líquida"
    elif pd.isna(nd_ebitda):
        p_div, det_div = None, "sem dados"
    else:
        p_div = _escala(nd_ebitda, [(1, 10), (2, 7), (3, 4)], maior_e_melhor=False)
        det_div = f"dívida líquida / EBITDA {nd_ebitda:.1f}x"
    if math.isnan(cob):
        p_cob, det_cob = None, "cobertura n/d"
    elif math.isinf(cob):
        p_cob, det_cob = 5, "sem juros"
    else:
        p_cob, det_cob = _escala(cob, [(8, 5), (5, 3), (3, 1)]), f"cobertura de juros {cob:.1f}x"
    if p_div is None and p_cob is None:
        add("Solidez do balanço", None, 15, "sem dados")
    else:
        maximo = (10 if p_div is not None else 0) + (5 if p_cob is not None else 0)
        add("Solidez do balanço", (p_div or 0) + (p_cob or 0), maximo, f"{det_div}; {det_cob}")

    ac = tab["acoes"].dropna()
    sbc = _mediana(tab["sbc_receita"])
    if len(ac) >= 2 and ac.iloc[0] > 0:
        anos = max((ac.index[-1] - ac.index[0]).days / 365.25, 1)
        var_anual = (ac.iloc[-1] / ac.iloc[0]) ** (1 / anos) - 1
        p_acoes = _escala(var_anual, [(-0.01, 6), (0.005, 5), (0.02, 2)], maior_e_melhor=False)
        det = f"número de ações {var_anual*100:+.1f}%/ano"
        p_sbc = 4 if math.isnan(sbc) else _escala(sbc, [(0.03, 4), (0.06, 2)], maior_e_melhor=False)
        det += "" if math.isnan(sbc) else f"; remuneração em ações {sbc*100:.1f}% das receitas"
        add("Diluição dos acionistas", p_acoes + p_sbc, 10, det)
    else:
        add("Diluição dos acionistas", None, 10, "sem dados")

    if pio["valor"] is None:
        add("Piotroski F-Score", None, 5, "sinais insuficientes")
    else:
        add("Piotroski F-Score", _escala(pio["valor"], [(7, 5), (5, 3)]), 5, f"{pio['valor']:.0f}/9")
    return comp


def _componentes_financeira(res: dict) -> list[dict]:
    tab = res["anual"]
    comp = []

    def add(nome, pontos, maximo, detalhe):
        comp.append({"componente": nome, "pontos": pontos, "maximo": maximo, "detalhe": detalhe})

    roe = tab["roe"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(roe):
        med = float(roe.median())
        add("Retorno sobre o capital próprio (ROE)", _escala(med, [(0.15, 30), (0.12, 22), (0.10, 15), (0.08, 8)]), 30,
            f"mediana {med*100:.1f}%")
        add("ROE consistente", _escala(float(roe.min()), [(0.08, 10), (0.05, 5)]), 10, f"pior ano {roe.min()*100:.1f}%")
    else:
        add("Retorno sobre o capital próprio (ROE)", None, 30, "sem dados")
        add("ROE consistente", None, 10, "sem dados")

    for nome, serie in [("EPS crescente", tab["eps"]), ("Valor contabilístico por ação crescente", tab["valor_contabilistico_acao"])]:
        t = tendencia(serie, "subir")
        pts = None if t["estado"] == "nd" else {"cumpre": 15, "parcial": 8, "falha": 0}[t["estado"]]
        add(nome, pts, 15, t["detalhe"])

    cp_at = (tab["capital_proprio"] / tab["ativo_total"]).dropna()
    if len(cp_at):
        v = float(cp_at.iloc[-1])
        add("Capital próprio / ativo", _escala(v, [(0.10, 10), (0.07, 6), (0.0, 2)]), 10,
            f"{v*100:.1f}% (almofada contra perdas)")
    else:
        add("Capital próprio / ativo", None, 10, "sem dados")

    ll = tab["lucro_liquido"].dropna()
    add("Lucro em todos os anos", (10 if (ll > 0).all() else 0) if len(ll) else None, 10,
        f"{int((ll > 0).sum())} de {len(ll)} anos com lucro" if len(ll) else "sem dados")

    ac = tab["acoes"].dropna()
    if len(ac) >= 2 and ac.iloc[0] > 0:
        var = ac.iloc[-1] / ac.iloc[0] - 1
        add("Diluição dos acionistas", _escala(var, [(0.0, 10), (0.05, 5)], maior_e_melhor=False), 10,
            f"número de ações {var*100:+.1f}% no período")
    else:
        add("Diluição dos acionistas", None, 10, "sem dados")
    return comp


# ---------------------------------------------------------------------------
# Alertas de armadilha de valor
# ---------------------------------------------------------------------------
def alertas(res: dict, alt: dict) -> list[dict]:
    tab = res["anual"]
    out = []

    def add(gravidade, texto):
        out.append({"gravidade": gravidade, "alerta": texto})

    rec = tab["receita"].dropna()
    if len(rec) >= 3:
        quedas = int((rec.diff().dropna().iloc[-3:] < 0).sum())
        if quedas >= 2:
            add("grave", f"Receitas caíram em {quedas} dos últimos 3 anos: o negócio pode estar em declínio estrutural.")
    if res.get("modelo_financeiro"):
        ll = tab["lucro_liquido"].dropna()
        if len(ll) and ll.iloc[-1] <= 0:
            add("grave", "Prejuízo no último exercício.")
        return out

    mb = tab["margem_bruta"].dropna()
    if len(mb) >= 3 and mb.iloc[-1] < mb.iloc[:-1].max() - 0.03:
        add("atenção", f"Margem bruta {(mb.iloc[:-1].max() - mb.iloc[-1])*100:.1f} p.p. abaixo do máximo recente: "
                       "possível perda de poder de fixação de preços.")
    roic = tab["roic"].dropna()
    if len(roic) and roic.iloc[-1] < 0.08 and not (tab["capital_investido_medio"].iloc[-1] <= 0):
        add("grave" if roic.iloc[-1] < 0.05 else "atenção",
            f"ROIC de {roic.iloc[-1]*100:.1f}% no último ano: abaixo do custo de capital, o crescimento destrói valor.")
    if alt["zona"] == "perigo":
        add("grave", f"Altman Z-Score {alt['valor']:.2f} (zona de perigo, abaixo de 1,81): risco financeiro elevado.")
    nd_e = tab["divida_liq_ebitda"].iloc[-1]
    if not pd.isna(nd_e) and nd_e > 3.5:
        add("grave", f"Dívida líquida de {nd_e:.1f}x o EBITDA.")
    cob = res["cobertura_atual"]
    if not math.isnan(cob) and not math.isinf(cob) and cob < 3:
        add("grave", f"Cobertura de juros de {cob:.1f}x: pouca margem se os resultados caírem.")
    conv = _mediana(tab["conversao_fcf"])
    if not math.isnan(conv) and conv < 0.6:
        add("atenção", f"O FCF é apenas {conv:.2f}x o lucro (mediana): lucros pouco convertidos em caixa.")
    f = tab["fcf"].dropna()
    if len(f) and f.iloc[-1] < 0:
        add("grave", "FCF negativo no último exercício.")
    ac = tab["acoes"].dropna()
    if len(ac) >= 2 and ac.iloc[0] > 0:
        anos = max((ac.index[-1] - ac.index[0]).days / 365.25, 1)
        v = (ac.iloc[-1] / ac.iloc[0]) ** (1 / anos) - 1
        if v > 0.03:
            add("grave", f"Número de ações a crescer {v*100:.1f}% ao ano: os acionistas estão a ser diluídos.")
    sbc = tab["sbc_receita"].dropna()
    if len(sbc) and sbc.iloc[-1] > 0.10:
        add("atenção", f"Remuneração em ações de {sbc.iloc[-1]*100:.1f}% das receitas: custo real escondido nos "
                       "resultados ajustados.")
    gw = (tab["goodwill"] / tab["ativo_total"]).dropna()
    if len(gw) and gw.iloc[-1] > 0.5:
        add("atenção", f"Goodwill e intangíveis são {gw.iloc[-1]*100:.0f}% do ativo: crescimento por aquisições, "
                       "risco de imparidades.")
    return out


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def avaliar_qualidade(res: dict) -> dict:
    pio = piotroski(res["anual"])
    alt = altman(res)
    comp = _componentes_financeira(res) if res.get("modelo_financeiro") else _componentes_nao_financeira(res, pio)
    avaliados = [c for c in comp if c["pontos"] is not None]
    maximo_total = sum(c["maximo"] for c in comp)
    maximo_avaliado = sum(c["maximo"] for c in avaliados)
    pontuacao = 100 * sum(c["pontos"] for c in avaliados) / maximo_avaliado if maximo_avaliado else None
    return {
        "pontuacao": pontuacao,
        "confianca": maximo_avaliado / maximo_total if maximo_total else 0.0,
        "componentes": comp,
        "piotroski": pio,
        "altman": alt,
        "alertas": alertas(res, alt),
    }
