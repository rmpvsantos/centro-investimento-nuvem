"""
Análise fundamental (quantitativa) por segmentos, sempre face à mediana dos concorrentes,
mais o apoio à avaliação qualitativa (gestão e moat).

Não depende do Streamlit nem do yfinance: recebe os dados em bruto e a análise já calculada.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from metricas import MIN_EMPRESAS_MEDIANA, alinhar, cagr, linha, ttm

NAN = float("nan")


# ---------------------------------------------------------------------------
# Estrutura da tabela: (rótulo, chave, formato, melhor, comparável)
#   formato: "milhoes", "pct", "x", "num"
#   melhor: "maior" | "menor" | None (neutro: mostra, mas não conta para a pontuação do segmento)
#   comparável: False para valores absolutos (dependem da dimensão da empresa): não há mediana
# ---------------------------------------------------------------------------
SEGMENTOS = [
    ("Operações", [
        ("Capitalização (milhões)", "cap", "milhoes", None, False),
        ("Variação da cotação a 1 ano", "cap_var_1a", "pct", "maior", True),
        ("Variação da cotação no período analisado", "cap_var_periodo", "pct", "maior", True),
        ("Receita (milhões)", "receita", "milhoes", None, False),
        ("Receita: crescimento anual composto", "receita_cagr", "pct", "maior", True),
        ("EPS diluído", "eps", "num", None, False),
        ("EPS: variação no período", "eps_var_total", "pct", "maior", True),
        ("EBITDA (milhões)", "ebitda", "milhoes", None, False),
        ("EBITDA: crescimento anual composto", "ebitda_cagr", "pct", "maior", True),
        ("FCF (milhões)", "fcf", "milhoes", None, False),
        ("FCF: crescimento anual composto", "fcf_cagr", "pct", "maior", True),
        ("Capex (milhões)", "capex", "milhoes", None, False),
        ("Capex: crescimento anual composto", "capex_cagr", "pct", None, True),
    ]),
    ("Rentabilidade e eficiência", [
        ("Margem bruta", "margem_bruta", "pct", "maior", True),
        ("Margem líquida", "margem_liquida", "pct", "maior", True),
        ("Margem FCF", "margem_fcf", "pct", "maior", True),
        ("Margem EBITDA", "margem_ebitda", "pct", "maior", True),
        ("Rotação do ativo (receita / ativo médio)", "rotacao_ativo", "x", "maior", True),
        ("ROE", "roe", "pct", "maior", True),
        ("ROA", "roa", "pct", "maior", True),
        ("ROIC", "roic", "pct", "maior", True),
        ("ROCE", "roce", "pct", "maior", True),
        ("WACC (estimado)", "wacc", "pct", "menor", True),
        ("Spread ROIC − WACC (pontos percentuais)", "spread", "pct", "maior", True),
        ("Caixa líquida (+) / dívida líquida (−) (milhões)", "caixa_liquida", "milhoes", None, False),
        ("Caixa líquida / capitalização", "caixa_liquida_cap", "pct", "maior", True),
    ]),
    ("Endividamento", [
        ("Dívida líquida / capital próprio", "nd_equity", "x", "menor", True),
        ("Dívida líquida / EBITDA", "nd_ebitda", "x", "menor", True),
        ("Dívida líquida / FCF", "nd_fcf", "x", "menor", True),
        ("Quick ratio", "quick", "x", "maior", True),
        ("Current ratio", "current", "x", "maior", True),
        ("Cobertura de juros", "cobertura", "x", "maior", True),
    ]),
    ("Valuation", [
        ("P/E atual", "pe", "x", "menor", True),
        ("PEG (P/E a dividir pelo crescimento anual do EPS)", "peg", "x", "menor", True),
        ("P/B", "pb", "x", "menor", True),
        ("EV/EBITDA", "ev_ebitda", "x", "menor", True),
        ("FCF por ação (bruto)", "fcf_ps", "num", None, False),
        ("FCF por ação, líquido do SBC", "fcf_ps_aj", "num", None, False),
        ("Rendimento do FCF, líquido do SBC", "fcf_yield_aj", "pct", "maior", True),
        ("SBC em percentagem do FCF", "sbc_fcf", "pct", "menor", True),
    ]),
    ("Proventos", [
        ("Buyback yield (recompras brutas / capitalização)", "buyback", "pct", "maior", True),
        ("Buyback yield, líquido do SBC", "buyback_liq", "pct", "maior", True),
        ("Dividend yield", "dividend_yield", "pct", "maior", True),
        ("Payout ratio (dividendos / lucro líquido)", "payout", "pct", None, True),
        ("Payout sobre o FCF", "payout_fcf", "pct", None, True),
        ("Rendimento total ao acionista, líquido do SBC", "yield_total", "pct", "maior", True),
        ("Variação do número de ações no período", "var_acoes", "pct", "menor", True),
    ]),
]

CHAVES = [c for _, linhas in SEGMENTOS for (_, c, *_r) in linhas]


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _ult(s) -> float:
    s = pd.Series(s).dropna()
    return float(s.iloc[-1]) if len(s) else NAN


def _media2(s) -> float:
    """Média dos dois últimos valores válidos (saldo médio); se só houver um, esse."""
    s = pd.Series(s).dropna()
    if not len(s):
        return NAN
    return float(s.iloc[-2:].mean())


def _div(a, b, positivo=True) -> float:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return NAN
    if positivo and b <= 0:
        return NAN
    if b == 0:
        return NAN
    return float(a) / float(b)


def _ok(v) -> bool:
    return v is not None and not pd.isna(v)


def _preco_em(precos: pd.Series | None, data) -> float:
    if precos is None or len(precos) == 0:
        return NAN
    p = precos.asof(pd.Timestamp(data))
    return float(p) if _ok(p) else NAN


def moedas_compativeis(info: dict) -> bool:
    mp, mf = info.get("currency"), info.get("financialCurrency")
    if not mp or not mf or mp == mf:
        return True
    return mp == "GBp" and mf == "GBP"


# ---------------------------------------------------------------------------
# Cálculo das métricas de uma empresa
# ---------------------------------------------------------------------------
def calcular(dados: dict, analise: dict, taxa_sem_risco: float, premio_risco: float) -> dict:
    info = dados.get("info") or {}
    bal, cfl, inc_q = dados.get("balance"), dados.get("cashflow"), dados.get("income_q")
    tab = analise["anual"]
    datas = tab.index
    notas: list[str] = []

    def serie(df, chave):
        return alinhar(linha(df, chave), datas)

    receita, lucro, ebitda = tab["receita"], tab["lucro_liquido"], tab["ebitda"]
    fcf, nd = tab["fcf"], tab["divida_liquida"]
    acoes = tab["acoes"]
    ativos = serie(bal, "ativos_totais")
    equity = serie(bal, "capital_proprio_comum")
    divida_total = serie(bal, "divida_total")
    ativo_corr = serie(bal, "ativo_corrente")
    passivo_corr = serie(bal, "passivo_corrente")
    inventario = serie(bal, "inventario")
    lucro_bruto = alinhar(linha(dados.get("income"), "lucro_bruto"), datas)
    capex = serie(cfl, "capex").abs()
    tem_cf = cfl is not None and not getattr(cfl, "empty", True)
    sbc = serie(cfl, "sbc").abs()
    recompras = serie(cfl, "recompras").abs()
    dividendos = serie(cfl, "dividendos").abs()

    # ---- capitalização e moedas ----------------------------------------------------------
    compat = moedas_compativeis(info)
    if not compat:
        notas.append(
            f"Moeda da cotação ({info.get('currency')}) diferente da moeda das demonstrações "
            f"({info.get('financialCurrency')}): as métricas que misturam preço e demonstrações não foram calculadas."
        )
    cap = info.get("marketCap")
    cap = float(cap) if _ok(cap) and cap > 0 else NAN
    if info.get("currency") == "GBp" and _ok(cap) and _ok(analise.get("preco_atual")) and _ok(_ult(acoes)):
        implicita = analise["preco_atual"] / 100.0 * _ult(acoes)
        if implicita > 0 and cap / implicita > 30:
            cap /= 100.0
            notas.append("Capitalização em pence convertida para libras.")
    cap_ok = cap if compat else NAN

    precos = dados.get("precos")
    if precos is not None and len(precos):
        precos = precos.copy()
        idx = pd.DatetimeIndex(pd.to_datetime(precos.index))
        precos.index = idx.tz_convert(None) if idx.tz is not None else idx
        precos = precos.dropna().sort_index()
    p_agora = float(precos.iloc[-1]) if precos is not None and len(precos) else NAN
    data_agora = precos.index[-1] if precos is not None and len(precos) else None
    cap_var_1a = _div(p_agora, _preco_em(precos, data_agora - pd.Timedelta(days=365)) if data_agora is not None else NAN) - 1
    cap_var_per = _div(p_agora, _preco_em(precos, datas[0])) - 1

    # ---- rentabilidade -------------------------------------------------------------------
    roic_serie = tab["roic"]
    roic = _ult(roic_serie)
    equity_med = _media2(equity)
    ativos_med = _media2(ativos)
    lucro_u, receita_u, fcf_u = _ult(lucro), _ult(receita), _ult(fcf)
    nd_u = _ult(nd)

    # WACC: CAPM para o capital próprio, custo efetivo da dívida e pesos a valores de mercado
    beta = info.get("beta")
    beta_yahoo = float(beta) if _ok(beta) and beta > 0 else NAN
    if not _ok(beta) or beta <= 0:
        beta = 1.0
        notas.append("Beta indisponível no Yahoo: usei 1,0 no cálculo do WACC.")
    ke = taxa_sem_risco + float(beta) * premio_risco
    juros_u = _ult(tab["juros"])
    kd = _div(juros_u, _media2(divida_total))
    if not _ok(kd):
        kd = taxa_sem_risco + 0.02
    kd = min(max(kd, 0.01), 0.15)
    t = _ult(tab["taxa_imposto"])
    t = t if _ok(t) else 0.21
    d_u = _ult(divida_total)
    d_u = d_u if _ok(d_u) else 0.0
    wacc = NAN
    if _ok(cap_ok):
        wacc = cap_ok / (cap_ok + d_u) * ke + d_u / (cap_ok + d_u) * kd * (1 - t)
    spread = roic - wacc if _ok(roic) and _ok(wacc) else NAN

    # ---- valuation -----------------------------------------------------------------------
    pe = analise["chaves"].get("pe_atual", NAN)
    cagr_eps = cagr(tab["eps"])
    peg = pe / (cagr_eps * 100) if _ok(pe) and _ok(cagr_eps) and cagr_eps > 0.005 else NAN
    eq_u = _ult(equity)
    pb = _div(cap_ok, eq_u)
    ebitda_ttm = ttm(inc_q, "ebitda")
    ebitda_ev = ebitda_ttm if _ok(ebitda_ttm) else _ult(ebitda)
    ev_ebitda = _div(cap_ok + nd_u, ebitda_ev) if _ok(cap_ok) and _ok(nd_u) else NAN
    sbc_u = _ult(sbc)
    if not _ok(sbc_u):
        sbc_u = 0.0
        notas.append("Sem linha de remuneração em ações (SBC) no Yahoo: tratada como zero, o que sobrevaloriza o FCF ajustado.")
    acoes_u = _ult(acoes)
    fcf_ps = _div(fcf_u, acoes_u)
    fcf_aj = fcf_u - sbc_u if _ok(fcf_u) else NAN
    fcf_ps_aj = _div(fcf_aj, acoes_u)

    # ---- proventos -----------------------------------------------------------------------
    rec_u = _ult(recompras)
    div_u = _ult(dividendos)
    if not _ok(rec_u):
        rec_u = 0.0 if tem_cf else NAN
    if not _ok(div_u):
        div_u = 0.0 if tem_cf else NAN
    if div_u == 0.0 and _ok(info.get("dividendRate")) and _ok(analise.get("preco_atual")) and analise["preco_atual"] > 0 and compat:
        dy_info = float(info["dividendRate"]) / float(analise["preco_atual"])
    else:
        dy_info = NAN
    dividend_yield = _div(div_u, cap_ok) if _ok(div_u) else dy_info
    if not _ok(dividend_yield) and _ok(dy_info):
        dividend_yield = dy_info

    preco_fin = analise.get("preco_atual", NAN)
    preco_fin = float(preco_fin) if _ok(preco_fin) else NAN
    if info.get("currency") == "GBp" and info.get("financialCurrency") == "GBP" and _ok(preco_fin):
        preco_fin /= 100.0

    v = {
        "cap": cap,
        "cap_var_1a": cap_var_1a,
        "cap_var_periodo": cap_var_per,
        "receita": receita_u,
        "receita_cagr": analise["chaves"].get("receita_cagr", NAN),
        "eps": _ult(tab["eps"]),
        "eps_var_total": analise["chaves"].get("eps_var_total", NAN),
        "ebitda": _ult(ebitda),
        "ebitda_cagr": analise["chaves"].get("ebitda_cagr", NAN),
        "fcf": fcf_u,
        "fcf_cagr": analise["chaves"].get("fcf_cagr", NAN),
        "capex": _ult(capex),
        "capex_cagr": cagr(capex),
        "margem_bruta": _div(_ult(lucro_bruto), receita_u),
        "margem_liquida": _div(lucro_u, receita_u),
        "margem_fcf": _ult(tab["margem_fcf"]),
        "margem_ebitda": _ult(tab["margem_ebitda"]),
        "rotacao_ativo": _div(receita_u, ativos_med),
        "roe": _div(lucro_u, equity_med),
        "roa": _div(lucro_u, ativos_med),
        "roic": roic,
        "roce": _ult(tab["roce"]),
        "wacc": wacc,
        "spread": spread,
        "caixa_liquida": -nd_u if _ok(nd_u) else NAN,
        "caixa_liquida_cap": (-nd_u / cap_ok) if _ok(nd_u) and _ok(cap_ok) else NAN,
        "nd_equity": (nd_u / eq_u) if _ok(nd_u) and _ok(eq_u) and eq_u > 0 else NAN,
        "nd_ebitda": analise["chaves"].get("nd_ebitda_atual", NAN),
        "nd_fcf": (nd_u / fcf_u) if _ok(nd_u) and _ok(fcf_u) and fcf_u > 0 else NAN,
        "quick": _div(_ult(ativo_corr) - (_ult(inventario) if _ok(_ult(inventario)) else 0.0), _ult(passivo_corr)),
        "current": _div(_ult(ativo_corr), _ult(passivo_corr)),
        "cobertura": analise.get("cobertura_atual", NAN),
        "pe": pe,
        "peg": peg,
        "pb": pb,
        "ev_ebitda": ev_ebitda,
        "fcf_ps": fcf_ps,
        "fcf_ps_aj": fcf_ps_aj,
        "fcf_yield_aj": (fcf_aj / cap_ok) if _ok(fcf_aj) and _ok(cap_ok) else NAN,
        "sbc_fcf": _div(sbc_u, fcf_u),
        "buyback": _div(rec_u, cap_ok),
        "buyback_liq": ((rec_u - sbc_u) / cap_ok) if _ok(rec_u) and _ok(cap_ok) else NAN,
        "dividend_yield": dividend_yield,
        "payout": _div(div_u, lucro_u),
        "payout_fcf": _div(div_u, fcf_u),
        "yield_total": ((div_u + rec_u - sbc_u) / cap_ok) if _ok(div_u) and _ok(rec_u) and _ok(cap_ok) else NAN,
        "var_acoes": analise["extras"].get("variacao_acoes_pct", NAN),
    }

    apoio = {
        "roic_medio": float(roic_serie.dropna().mean()) if roic_serie.notna().any() else NAN,
        "margem_ebitda_var": (
            float(tab["margem_ebitda"].dropna().iloc[-1] - tab["margem_ebitda"].dropna().iloc[0])
            if tab["margem_ebitda"].notna().sum() >= 2 else NAN
        ),
        "beta": float(beta),
        "beta_yahoo": beta_yahoo,
        "custo_capital_proprio": ke,
        "custo_divida": kd,
        # Base para o DCF (recalculável na interface sem voltar a ir ao Yahoo)
        "taxa_sem_risco": taxa_sem_risco,
        "wacc": wacc,
        "fcf_base": fcf_aj,
        "fcf_bruto": fcf_u,
        "sbc": sbc_u,
        "acoes": acoes_u,
        "divida_liquida": nd_u,
        "preco": preco_fin,
        "moeda_ok": compat,
        "moeda_fin": info.get("financialCurrency") or info.get("currency"),
        "cagr_fcf": cagr(fcf),
        "ebitda_base": ebitda_ev,
        "ebitda_ttm": bool(_ok(ebitda_ttm)),
        "cagr_ebitda": analise["chaves"].get("ebitda_cagr", NAN),
        "ev_ebitda": ev_ebitda,
        "pe": pe,
        "eps_base": (preco_fin / pe) if _ok(preco_fin) and _ok(pe) and pe > 0 else NAN,
        "cagr_receita": analise["chaves"].get("receita_cagr", NAN),
        "setor": analise.get("setor"),
    }
    return {
        "ticker": analise.get("ticker"),
        "nome": analise.get("nome"),
        "moeda": analise.get("moeda"),
        "setor": analise.get("setor"),
        "industria": analise.get("industria"),
        "valores": v,
        "apoio": apoio,
        "notas": notas,
        "dirigentes": dirigentes(info),
        "governanca": governanca(info),
    }


# ---------------------------------------------------------------------------
# Comparação com a mediana dos concorrentes
# ---------------------------------------------------------------------------
def _valido(v) -> bool:
    return _ok(v) and not (isinstance(v, float) and math.isinf(v))


def mediana_grupo(valores_pares: list[dict], chave: str) -> tuple[float, int]:
    vals = [float(p[chave]) for p in valores_pares if chave in p and _valido(p[chave])]
    if len(vals) < MIN_EMPRESAS_MEDIANA:
        return NAN, len(vals)
    return float(np.median(vals)), len(vals)


def face_mediana(valor, mediana, melhor) -> str:
    """'melhor' | 'pior' | 'igual' | 'neutro' | 'nd'."""
    if melhor is None:
        return "neutro"
    if not _ok(mediana) or not _ok(valor):
        return "nd"
    if isinstance(valor, float) and math.isinf(valor):
        return "melhor" if melhor == "maior" else "pior"
    if abs(valor - mediana) < 1e-12:
        return "igual"
    if melhor == "maior":
        return "melhor" if valor > mediana else "pior"
    return "melhor" if valor < mediana else "pior"


def avaliar(valores_empresa: dict, valores_pares: list[dict]) -> list[dict]:
    """Por segmento: cada métrica com a mediana dos concorrentes e a posição relativa."""
    saida = []
    for nome, linhas in SEGMENTOS:
        itens, melhores, total = [], 0.0, 0
        for rotulo, chave, fmt, melhor, comparavel in linhas:
            val = valores_empresa.get(chave, NAN)
            med, n = mediana_grupo(valores_pares, chave) if comparavel else (NAN, 0)
            face = face_mediana(val, med, melhor) if comparavel else "neutro"
            if face in ("melhor", "pior", "igual"):
                total += 1
                melhores += 1.0 if face == "melhor" else 0.5 if face == "igual" else 0.0
            itens.append({"rotulo": rotulo, "chave": chave, "fmt": fmt, "valor": val, "mediana": med,
                          "n": n, "face": face, "comparavel": comparavel, "melhor": melhor})
        saida.append({"nome": nome, "linhas": itens, "melhores": melhores, "total": total,
                      "pct": melhores / total if total else NAN})
    return saida


def resumo_quantitativo(segmentos: list[dict]) -> dict:
    m = sum(s["melhores"] for s in segmentos)
    t = sum(s["total"] for s in segmentos)
    return {"melhores": m, "total": t, "pct": m / t if t else NAN}


# ---------------------------------------------------------------------------
# Qualitativo: dirigentes (Yahoo) e indicador quantitativo de moat
# ---------------------------------------------------------------------------
def dirigentes(info: dict) -> dict:
    """Presidente, CEO e CFO a partir de companyOfficers do Yahoo (pode faltar ou vir incompleto)."""
    out = {"presidente": None, "ceo": None, "cfo": None}
    for o in info.get("companyOfficers") or []:
        titulo = str(o.get("title") or "")
        t = titulo.lower()
        nome = o.get("name")
        if not nome:
            continue
        item = {"nome": nome, "titulo": titulo, "idade": o.get("age"), "remuneracao": o.get("totalPay")}
        if out["ceo"] is None and ("chief executive" in t or t.startswith("ceo") or " ceo" in t):
            out["ceo"] = item
        if out["cfo"] is None and ("chief financial" in t or t.startswith("cfo") or " cfo" in t):
            out["cfo"] = item
        if out["presidente"] is None and "chair" in t:
            out["presidente"] = item
    return out


def indicador_moat(valores: dict, apoio: dict, mediana_margem_bruta: float = NAN) -> dict:
    """
    Indicador quantitativo de moat de 0 a 5 (proxy, não substitui a análise do negócio).
    Componentes: ROIC médio >= 15%; ROIC médio >= 25%; ROIC acima do WACC; margem bruta acima da
    mediana dos concorrentes; margem EBITDA estável ou a subir (não cai mais de 2 pontos percentuais).
    """
    comp: list[tuple[str, bool | None]] = []
    rm = apoio.get("roic_medio", NAN)
    comp.append(("ROIC médio do período igual ou superior a 15%", None if not _ok(rm) else rm >= 0.15))
    comp.append(("ROIC médio do período igual ou superior a 25%", None if not _ok(rm) else rm >= 0.25))
    sp = valores.get("spread", NAN)
    comp.append(("ROIC acima do WACC estimado", None if not _ok(sp) else sp > 0))
    mb = valores.get("margem_bruta", NAN)
    comp.append(("Margem bruta acima da mediana dos concorrentes",
                 None if not (_ok(mb) and _ok(mediana_margem_bruta)) else mb > mediana_margem_bruta))
    mv = apoio.get("margem_ebitda_var", NAN)
    comp.append(("Margem EBITDA estável ou a subir (queda inferior a 2 pontos percentuais)",
                 None if not _ok(mv) else mv > -0.02))
    avaliados = [c for c in comp if c[1] is not None]
    if len(avaliados) < 3:
        return {"nota": NAN, "componentes": comp}
    nota = sum(1 for c in avaliados if c[1]) / len(avaliados) * 5
    return {"nota": round(nota * 2) / 2, "componentes": comp}


def governanca(info: dict) -> dict:
    """
    Indicadores de governação que o Yahoo publica (riscos de 1 a 10, em que 10 é o pior, atribuídos pela ISS) e
    a percentagem do capital detida por insiders e por instituições. Qualquer campo pode faltar.
    """
    def num(chave):
        v = info.get(chave)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return NAN
        return v if _ok(v) else NAN
    return {
        "risco_geral": num("overallRisk"),
        "risco_auditoria": num("auditRisk"),
        "risco_conselho": num("boardRisk"),
        "risco_remuneracao": num("compensationRisk"),
        "risco_direitos_acionistas": num("shareHolderRightsRisk"),
        "insiders": num("heldPercentInsiders"),
        "instituicoes": num("heldPercentInstitutions"),
    }


# ---------------------------------------------------------------------------
# Valor intrínseco por fluxos de caixa descontados (DCF)
# ---------------------------------------------------------------------------
ANOS_DCF = 5
CRESCIMENTO_MAX_DCF = 0.12
CRESCIMENTO_TERMINAL_PADRAO = 0.025
FOLGA_MINIMA_TAXA = 0.01  # a taxa de desconto tem de exceder o crescimento terminal em pelo menos 1 ponto percentual


def pressupostos_dcf(apoio: dict) -> dict:
    """
    Pressupostos iniciais (o utilizador pode alterá-los). Crescimento do FCF: o crescimento anual composto histórico,
    limitado a 0%–12% (4 exercícios são pouco para extrapolar taxas extremas). Sem esse dado, usa o da receita e,
    sem este, 3%. Crescimento terminal: 2,5%, nunca acima da taxa sem risco.
    """
    g, origem = apoio.get("cagr_fcf", NAN), "crescimento anual composto do FCF no período"
    if not _ok(g):
        g, origem = apoio.get("cagr_receita", NAN), "crescimento anual composto da receita (o FCF não tem histórico utilizável)"
    if not _ok(g):
        g, origem = 0.03, "valor por omissão (sem histórico utilizável)"
    g_lim = min(max(float(g), 0.0), CRESCIMENTO_MAX_DCF)
    if g_lim != g:
        origem += f", limitado ao intervalo 0%–{CRESCIMENTO_MAX_DCF:.0%}"
    rf = apoio.get("taxa_sem_risco", NAN)
    gt = min(CRESCIMENTO_TERMINAL_PADRAO, rf) if _ok(rf) else CRESCIMENTO_TERMINAL_PADRAO
    wacc = apoio.get("wacc", NAN)
    return {"crescimento": g_lim, "origem_crescimento": origem, "crescimento_terminal": gt,
            "taxa_desconto": wacc if _ok(wacc) else NAN}


def _projetar(fcf0: float, crescimento: float, taxa: float, anos: int, desvanecer_apos: int | None = None,
              crescimento_final: float = 0.0) -> tuple[list[dict], float, float]:
    """
    Fluxos dos `anos` anos, com valor presente. Devolve (fluxos, soma dos valores presentes, fluxo do último ano).
    Com `desvanecer_apos`, o crescimento mantém-se até esse ano e desce em linha reta até `crescimento_final` no último ano.
    """
    fluxos, vp_total, fcf = [], 0.0, fcf0
    for ano in range(1, anos + 1):
        g = crescimento
        if desvanecer_apos is not None and ano > desvanecer_apos and anos > desvanecer_apos:
            g = crescimento + (crescimento_final - crescimento) * (ano - desvanecer_apos) / (anos - desvanecer_apos)
        fcf *= 1 + g
        vp = fcf / (1 + taxa) ** ano
        vp_total += vp
        fluxos.append({"ano": ano, "fcf": fcf, "vp": vp})
    return fluxos, vp_total, fcf


def dcf(apoio: dict, taxa_desconto: float, crescimento: float, crescimento_terminal: float, anos: int = ANOS_DCF,
        desvanecer_apos: int | None = None) -> dict:
    """
    FCF ajustado do SBC do último exercício, projetado `anos` anos ao ritmo `crescimento`, mais valor terminal
    de Gordon; tudo descontado à `taxa_desconto`. Valor do capital = valor da empresa − dívida líquida; por ação
    com o número de ações do último exercício. Devolve {"ok": False, "motivo": ...} quando não faz sentido calcular.
    """
    def falha(motivo):
        return {"ok": False, "motivo": motivo, "valor_acao": NAN, "margem": NAN, "potencial": NAN}

    if not apoio.get("moeda_ok", True):
        return falha("a moeda da cotação difere da das demonstrações financeiras")
    fcf0 = apoio.get("fcf_base", NAN)
    if not _ok(fcf0):
        return falha("sem FCF no Yahoo")
    if fcf0 <= 0:
        return falha("o FCF do último exercício, depois de retirar o SBC, é negativo ou nulo: um DCF sobre fluxos negativos não faz sentido")
    acoes, nd = apoio.get("acoes", NAN), apoio.get("divida_liquida", NAN)
    if not _ok(acoes) or acoes <= 0:
        return falha("sem número de ações")
    if not _ok(nd):
        return falha("sem dívida líquida")
    if not _ok(taxa_desconto):
        return falha("sem taxa de desconto (WACC indisponível)")
    if taxa_desconto - crescimento_terminal < FOLGA_MINIMA_TAXA:
        return falha("a taxa de desconto tem de superar o crescimento terminal em pelo menos 1 ponto percentual")

    fluxos, vp_expl, fcf = _projetar(float(fcf0), crescimento, taxa_desconto, anos, desvanecer_apos, crescimento_terminal)
    valor_term = fcf * (1 + crescimento_terminal) / (taxa_desconto - crescimento_terminal)  # fcf é o do último ano projetado
    vp_term = valor_term / (1 + taxa_desconto) ** anos
    ev = vp_expl + vp_term
    capital = ev - nd
    if capital <= 0:
        return falha("o valor do capital próprio resulta negativo depois de retirar a dívida líquida")
    valor_acao = capital / acoes
    preco = apoio.get("preco", NAN)
    margem = 1 - preco / valor_acao if _ok(preco) and preco > 0 else NAN
    potencial = valor_acao / preco - 1 if _ok(preco) and preco > 0 else NAN
    return {"ok": True, "motivo": None, "valor_acao": valor_acao, "valor_empresa": ev, "valor_capital": capital,
            "vp_explicito": vp_expl, "vp_terminal": vp_term, "peso_terminal": vp_term / ev, "fluxos": fluxos,
            "preco": preco, "margem": margem, "potencial": potencial, "taxa": taxa_desconto,
            "crescimento": crescimento, "crescimento_terminal": crescimento_terminal, "anos": anos}


def sensibilidade_dcf(apoio: dict, taxa: float, crescimento: float, crescimento_terminal: float,
                      passos_taxa=(-0.02, -0.01, 0.0, 0.01, 0.02),
                      passos_gt=(-0.01, -0.005, 0.0, 0.005, 0.01)) -> dict:
    """Valor por ação para combinações de taxa de desconto (linhas) e crescimento terminal (colunas)."""
    taxas = [taxa + d for d in passos_taxa]
    gts = [crescimento_terminal + d for d in passos_gt]
    matriz = [[dcf(apoio, t, crescimento, g)["valor_acao"] for g in gts] for t in taxas]
    return {"taxas": taxas, "crescimentos_terminais": gts, "matriz": matriz}


# ---------------------------------------------------------------------------
# Vale a pena partir para um relatório de iniciação?
# ---------------------------------------------------------------------------
def _v1(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def recomendacao_iniciacao(triagem: dict, resumo: dict, valores: dict, segmentos: list[dict], dcf_res: dict | None = None,
                           valorizacao: dict | None = None) -> dict:
    """
    Regra de triagem (cortes arbitrários, não é recomendação de investimento). Até 5 pontos:
      - triagem individual: "sim" = 2, "reservas" = 1, "nao" = 0;
      - percentagem de métricas melhores que a mediana dos concorrentes: >= 60% = 2, >= 45% = 1, abaixo = 0
        (só conta com pelo menos 10 métricas comparáveis);
      - spread ROIC − WACC positivo = 1.
    O que falta de dados sai do denominador. Nível: "sim" com 80% ou mais dos pontos possíveis; "reservas" com 40% ou mais;
    "nao" abaixo. Se a triagem individual for "nao", o nível nunca passa de "reservas". O preço/DCF não entra na pontuação:
    um preço exigente muda o foco do relatório, não a decisão de o fazer.
    """
    pts, maximo, pontos = 0.0, 0.0, []
    nivel_tri = triagem.get("nivel")
    pts_tri = {"sim": 2, "reservas": 1, "nao": 0}.get(nivel_tri)
    if pts_tri is not None:
        pts += pts_tri
        maximo += 2
        pontos.append(f"Triagem individual: {triagem.get('titulo', '').lower()} ({pts_tri} de 2 pontos).")
    pct, total = resumo.get("pct", NAN), resumo.get("total", 0)
    if total >= 10 and _ok(pct):
        p = 2 if pct >= 0.60 else 1 if pct >= 0.45 else 0
        pts += p
        maximo += 2
        pontos.append(f"Melhor que a mediana dos concorrentes em {pct * 100:.0f}% das {total} métricas comparáveis ({p} de 2 pontos).")
    else:
        pontos.append("Comparação com os concorrentes sem dados suficientes: não entra na decisão.")
    sp = valores.get("spread", NAN)
    if _ok(sp):
        p = 1 if sp > 0 else 0
        pts += p
        maximo += 1
        pontos.append(
            f"ROIC {'acima' if sp > 0 else 'abaixo'} do WACC estimado em {_v1(abs(sp) * 100)} pontos percentuais ({p} de 1 ponto)."
        )
    ratio = pts / maximo if maximo else NAN
    if not _ok(ratio):
        nivel = "nd"
    elif ratio >= 0.8:
        nivel = "sim"
    elif ratio >= 0.4:
        nivel = "reservas"
    else:
        nivel = "nao"
    if nivel == "sim" and nivel_tri == "nao":
        nivel = "reservas"
    titulos = {
        "sim": "Vale a pena partir para um relatório de iniciação",
        "reservas": "Vale a pena, mas o relatório deve começar pelas reservas",
        "nao": "Não compensa um relatório de iniciação para já",
        "nd": "Dados insuficientes para decidir",
    }
    # Onde o relatório deve investir tempo: segmentos abaixo de 50% face à mediana
    focos = [f"{s['nome']} ({_v1(s['melhores'])} de {s['total']} métricas melhores que a mediana)"
             for s in segmentos if s["total"] >= 3 and _ok(s["pct"]) and s["pct"] < 0.5]
    if dcf_res is not None:
        if dcf_res.get("ok") and _ok(dcf_res.get("margem")):
            m = dcf_res["margem"]
            if m >= 0:
                pontos.append(f"DCF com os pressupostos escolhidos: preço {m * 100:.0f}% abaixo do valor intrínseco estimado (informativo, não pontua).")
            else:
                pontos.append(
                    f"DCF com os pressupostos escolhidos: preço {(dcf_res['preco'] / dcf_res['valor_acao'] - 1) * 100:.0f}% acima do valor "
                    "intrínseco estimado (informativo, não pontua). O relatório deve testar que crescimento o mercado está a assumir."
                )
        elif not dcf_res.get("ok"):
            pontos.append(f"DCF sem valor: {dcf_res.get('motivo')}.")
    if valorizacao is not None:
        if valorizacao.get("ok"):
            dif = valorizacao["mediana"] / valorizacao["preco"] - 1 if valorizacao.get("preco") else float("nan")
            face = "" if not _ok(dif) else f", {_v1(abs(dif) * 100)}% {'acima' if dif >= 0 else 'abaixo'} do preço atual"
            pontos.append(
                f"Valor intrínseco de referência (mediana de {valorizacao['n']} valores): {_v1(valorizacao['mediana'])} por ação{face} "
                "(informativo, não pontua)."
            )
        elif valorizacao.get("motivo"):
            pontos.append(f"Valor intrínseco de referência sem resultado: {valorizacao['motivo']}.")
    return {"nivel": nivel, "titulo": titulos[nivel], "pontos": pontos, "focos": focos, "pts": pts, "maximo": maximo,
            "pct": ratio}


# ---------------------------------------------------------------------------
# DCF por múltiplos (EV/EBITDA de saída), DCF invertido e intervalo de compra
# ---------------------------------------------------------------------------
def pressupostos_multiplo(apoio: dict, mediana_ev_ebitda: float = NAN) -> dict:
    """
    Crescimento do EBITDA: o histórico, limitado a 0%–12%. Múltiplo de saída: a mediana do EV/EBITDA dos concorrentes;
    sem ela, o EV/EBITDA atual da própria empresa; sem nenhum, NaN (o utilizador tem de o indicar).
    """
    g = apoio.get("cagr_ebitda", NAN)
    origem_g = "crescimento anual composto do EBITDA no período"
    if not _ok(g):
        g, origem_g = pressupostos_dcf(apoio)["crescimento"], "o mesmo crescimento do FCF (o EBITDA não tem histórico utilizável)"
    g_lim = min(max(float(g), 0.0), CRESCIMENTO_MAX_DCF)
    if g_lim != g:
        origem_g += f", limitado ao intervalo 0%–{CRESCIMENTO_MAX_DCF:.0%}"
    if _ok(mediana_ev_ebitda) and mediana_ev_ebitda > 0:
        m, origem_m = float(mediana_ev_ebitda), "mediana do EV/EBITDA dos concorrentes"
    elif _ok(apoio.get("ev_ebitda", NAN)) and apoio["ev_ebitda"] > 0:
        m, origem_m = float(apoio["ev_ebitda"]), "EV/EBITDA atual da própria empresa (sem mediana dos concorrentes)"
    else:
        m, origem_m = NAN, "sem referência disponível"
    return {"crescimento_ebitda": g_lim, "origem_crescimento": origem_g, "multiplo": m, "origem_multiplo": origem_m}


def dcf_multiplo(apoio: dict, taxa_desconto: float, crescimento_fcf: float, crescimento_ebitda: float, multiplo: float,
                 anos: int = ANOS_DCF) -> dict:
    """
    Fluxos de caixa dos `anos` anos (FCF líquido do SBC, ao crescimento indicado; se o FCF de partida for nulo ou negativo,
    os fluxos intermédios valem zero) mais valor de saída = EBITDA do último ano projetado × múltiplo EV/EBITDA; tudo
    descontado à taxa de desconto. Depois retira-se a dívida líquida e divide-se pelas ações.
    """
    def falha(motivo):
        return {"ok": False, "motivo": motivo, "valor_acao": NAN, "margem": NAN, "potencial": NAN}

    if not apoio.get("moeda_ok", True):
        return falha("a moeda da cotação difere da das demonstrações financeiras")
    e0 = apoio.get("ebitda_base", NAN)
    if not _ok(e0) or e0 <= 0:
        return falha("EBITDA em falta, nulo ou negativo")
    if not _ok(multiplo) or multiplo <= 0:
        return falha("sem múltiplo EV/EBITDA de saída")
    acoes, nd = apoio.get("acoes", NAN), apoio.get("divida_liquida", NAN)
    if not _ok(acoes) or acoes <= 0:
        return falha("sem número de ações")
    if not _ok(nd):
        return falha("sem dívida líquida")
    if not _ok(taxa_desconto) or taxa_desconto <= 0:
        return falha("sem taxa de desconto (WACC indisponível)")
    fcf0 = apoio.get("fcf_base", NAN)
    fcf_zero = not _ok(fcf0) or fcf0 <= 0
    fluxos, vp_expl, _ = _projetar(0.0 if fcf_zero else float(fcf0), crescimento_fcf, taxa_desconto, anos)
    ebitda_final = float(e0) * (1 + crescimento_ebitda) ** anos
    ev_saida = ebitda_final * multiplo
    vp_saida = ev_saida / (1 + taxa_desconto) ** anos
    ev = vp_expl + vp_saida
    capital = ev - nd
    if capital <= 0:
        return falha("o valor do capital próprio resulta negativo depois de retirar a dívida líquida")
    valor_acao = capital / acoes
    preco = apoio.get("preco", NAN)
    margem = 1 - preco / valor_acao if _ok(preco) and preco > 0 else NAN
    potencial = valor_acao / preco - 1 if _ok(preco) and preco > 0 else NAN
    return {"ok": True, "motivo": None, "valor_acao": valor_acao, "valor_empresa": ev, "valor_capital": capital,
            "vp_explicito": vp_expl, "vp_terminal": vp_saida, "peso_terminal": vp_saida / ev, "fluxos": fluxos,
            "ebitda_final": ebitda_final, "ev_saida": ev_saida, "fcf_zero": fcf_zero, "preco": preco, "margem": margem,
            "potencial": potencial, "taxa": taxa_desconto, "crescimento_fcf": crescimento_fcf,
            "crescimento_ebitda": crescimento_ebitda, "multiplo": multiplo, "anos": anos}


def sensibilidade_multiplo(apoio: dict, taxa: float, crescimento_fcf: float, crescimento_ebitda: float, multiplo: float,
                           fatores_multiplo=(0.8, 0.9, 1.0, 1.1, 1.2),
                           passos_g=(-0.04, -0.02, 0.0, 0.02, 0.04)) -> dict:
    """Valor por ação para o múltiplo de saída (linhas) e o crescimento do EBITDA (colunas)."""
    multiplos = [multiplo * f for f in fatores_multiplo]
    gs = [max(crescimento_ebitda + d, -0.5) for d in passos_g]
    matriz = [[dcf_multiplo(apoio, taxa, crescimento_fcf, g, m)["valor_acao"] for g in gs] for m in multiplos]
    return {"linhas": multiplos, "colunas": gs, "matriz": matriz}


def dcf_invertido(apoio: dict, taxa_desconto: float, crescimento_terminal: float, anos: int = ANOS_DCF,
                  g_min: float = -0.30, g_max: float = 0.60) -> dict:
    """
    Que crescimento anual do FCF (líquido do SBC), durante `anos` anos, faz o DCF igualar o preço atual, mantendo a taxa de
    desconto e o crescimento a perpetuidade? Resolve-se por bisseção. `limite` indica se o preço exige mais do que g_max
    ("acima") ou menos do que g_min ("abaixo").
    """
    def falha(motivo):
        return {"ok": False, "motivo": motivo}

    if not apoio.get("moeda_ok", True):
        return falha("a moeda da cotação difere da das demonstrações financeiras")
    fcf0, preco = apoio.get("fcf_base", NAN), apoio.get("preco", NAN)
    if not _ok(fcf0) or fcf0 <= 0:
        return falha("o FCF do último exercício, depois de retirar o SBC, é negativo, nulo ou não existe")
    if not _ok(preco) or preco <= 0:
        return falha("sem preço atual")
    acoes, nd = apoio.get("acoes", NAN), apoio.get("divida_liquida", NAN)
    if not _ok(acoes) or acoes <= 0 or not _ok(nd):
        return falha("sem número de ações ou dívida líquida")
    if not _ok(taxa_desconto) or taxa_desconto - crescimento_terminal < FOLGA_MINIMA_TAXA:
        return falha("a taxa de desconto tem de superar o crescimento a perpetuidade em pelo menos 1 ponto percentual")

    def valor(g):
        _, vp_expl, ultimo = _projetar(float(fcf0), g, taxa_desconto, anos)
        vp_term = ultimo * (1 + crescimento_terminal) / (taxa_desconto - crescimento_terminal) / (1 + taxa_desconto) ** anos
        return (vp_expl + vp_term - nd) / acoes

    if valor(g_max) < preco:
        return {"ok": True, "crescimento": g_max, "limite": "acima", "preco": preco, "motivo": None}
    if valor(g_min) > preco:
        return {"ok": True, "crescimento": g_min, "limite": "abaixo", "preco": preco, "motivo": None}
    lo, hi = g_min, g_max
    for _ in range(80):
        meio = (lo + hi) / 2
        if valor(meio) < preco:
            lo = meio
        else:
            hi = meio
    return {"ok": True, "crescimento": (lo + hi) / 2, "limite": None, "preco": preco, "motivo": None}


def intervalo_compra(valores: dict, margem_seguranca: float, preco: float) -> dict:
    """
    valores: {"método": valor por ação} (só os que foram calculados). Valor central = média dos métodos; conservador = o mais baixo.
    Intervalo de compra = de (valor conservador × (1 − margem)) a (valor central × (1 − margem)). Se só há um método, o intervalo
    reduz-se a um ponto. Compara o preço atual com o intervalo.
    """
    v = {k: x for k, x in valores.items() if _ok(x) and x > 0}
    if not v:
        return {"ok": False, "motivo": "nenhum método deu valor"}
    baixo, alto = min(v.values()), max(v.values())
    central = sum(v.values()) / len(v)
    piso, teto = baixo * (1 - margem_seguranca), central * (1 - margem_seguranca)
    if not _ok(preco) or preco <= 0:
        posicao, dist = "nd", NAN
    elif preco <= piso:
        posicao, dist = "abaixo", NAN
    elif preco <= teto:
        posicao, dist = "dentro", NAN
    else:
        posicao, dist = "acima", 1 - teto / preco  # descida de preço necessária para chegar ao teto
    return {"ok": True, "motivo": None, "metodos": v, "baixo": baixo, "alto": alto, "central": central,
            "piso": piso, "teto": teto, "margem_seguranca": margem_seguranca, "posicao": posicao,
            "descida_ate_teto": dist, "preco": preco, "ponto_unico": len(v) == 1 or abs(piso - teto) < 1e-9}


# ---------------------------------------------------------------------------
# Múltiplos relativos: aplicar o EV/EBITDA ou o P/E de referência aos resultados atuais
# ---------------------------------------------------------------------------
TIPOS_MULTIPLO = {"ev_ebitda": "EV/EBITDA", "pe": "P/E"}


def multiplo_referencia(apoio: dict, tipo: str, mediana: float = NAN) -> tuple[float, str]:
    """Múltiplo por omissão: mediana dos concorrentes; sem ela, o múltiplo atual da própria empresa (o que devolve o preço atual)."""
    if _ok(mediana) and mediana > 0:
        return float(mediana), "mediana dos concorrentes"
    atual = apoio.get(tipo, NAN)
    if _ok(atual) and atual > 0:
        return float(atual), "múltiplo atual da própria empresa (sem mediana dos concorrentes)"
    return NAN, "sem referência disponível"


def valor_por_multiplo(apoio: dict, tipo: str, multiplo: float, ajuste_base: float = 0.0) -> dict:
    """
    EV/EBITDA: (EBITDA × múltiplo − dívida líquida) ÷ ações. P/E: EPS × múltiplo. Usa os resultados dos últimos 12 meses
    (EBITDA) ou o EPS implícito no P/E atual. `ajuste_base` altera a base (por exemplo +10% de EBITDA) para a sensibilidade.
    """
    def falha(motivo):
        return {"ok": False, "motivo": motivo, "valor_acao": NAN, "margem": NAN, "potencial": NAN}

    if tipo not in TIPOS_MULTIPLO:
        return falha("tipo de múltiplo desconhecido")
    if not apoio.get("moeda_ok", True):
        return falha("a moeda da cotação difere da das demonstrações financeiras")
    if not _ok(multiplo) or multiplo <= 0:
        return falha(f"sem múltiplo {TIPOS_MULTIPLO[tipo]} de referência")
    preco = apoio.get("preco", NAN)
    if tipo == "ev_ebitda":
        base = apoio.get("ebitda_base", NAN)
        if not _ok(base) or base <= 0:
            return falha("EBITDA em falta, nulo ou negativo")
        acoes, nd = apoio.get("acoes", NAN), apoio.get("divida_liquida", NAN)
        if not _ok(acoes) or acoes <= 0 or not _ok(nd):
            return falha("sem número de ações ou dívida líquida")
        base *= 1 + ajuste_base
        ev = base * multiplo
        capital = ev - nd
        if capital <= 0:
            return falha("o valor do capital próprio resulta negativo depois de retirar a dívida líquida")
        valor = capital / acoes
        extra = {"valor_empresa": ev, "valor_capital": capital}
    else:
        base = apoio.get("eps_base", NAN)
        if not _ok(base) or base <= 0:
            return falha("EPS em falta, nulo ou negativo (o P/E não se aplica)")
        base *= 1 + ajuste_base
        valor = base * multiplo
        extra = {}
    margem = 1 - preco / valor if _ok(preco) and preco > 0 else NAN
    potencial = valor / preco - 1 if _ok(preco) and preco > 0 else NAN
    return {"ok": True, "motivo": None, "valor_acao": valor, "preco": preco, "margem": margem, "potencial": potencial,
            "tipo": tipo, "multiplo": multiplo, "base": base, **extra}


def sensibilidade_relativa(apoio: dict, tipo: str, multiplo: float, fatores_multiplo=(0.8, 0.9, 1.0, 1.1, 1.2),
                           ajustes_base=(-0.2, -0.1, 0.0, 0.1, 0.2)) -> dict:
    """Valor por ação para o múltiplo (linhas) e a variação da base, EBITDA ou EPS (colunas)."""
    multiplos = [multiplo * f for f in fatores_multiplo]
    matriz = [[valor_por_multiplo(apoio, tipo, m, a)["valor_acao"] for a in ajustes_base] for m in multiplos]
    return {"linhas": multiplos, "colunas": list(ajustes_base), "matriz": matriz}
