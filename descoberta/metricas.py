"""
Motor de cálculo da triagem de ações.

Recebe os dados em bruto (demonstrações financeiras, preços, splits, info) e
devolve as métricas e os critérios. Não depende do Streamlit nem do yfinance,
o que permite testar a lógica com dados simulados.

Convenção de moedas: todos os valores de avaliação são calculados na moeda das
demonstrações financeiras ("moeda financeira"). O preço é convertido para essa
moeda (pence → libras, e câmbio histórico quando a cotação é noutra moeda).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Nomes das linhas nas demonstrações do Yahoo Finance (primeiro que existir)
# ---------------------------------------------------------------------------
ROTULOS = {
    "receita": ["Total Revenue", "Operating Revenue", "Revenue"],
    "lucro_bruto": ["Gross Profit"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "ebit": ["EBIT", "Operating Income"],
    "juros": ["Interest Expense", "Interest Expense Non Operating"],
    "lucro_liquido": ["Net Income Common Stockholders", "Net Income"],
    "taxa_imposto": ["Tax Rate For Calcs"],
    "eps": ["Diluted EPS", "Basic EPS"],
    "acoes_diluidas": ["Diluted Average Shares", "Basic Average Shares"],
    "fcf": ["Free Cash Flow"],
    "cfo": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure"],
    "sbc": ["Stock Based Compensation"],
    "dividendos": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    "recompras": ["Repurchase Of Capital Stock", "Common Stock Payments"],
    "divida_liquida": ["Net Debt"],
    "divida_total": ["Total Debt"],
    "caixa": ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"],
    "ativo_total": ["Total Assets"],
    "ativo_corrente": ["Current Assets"],
    "passivo_corrente": ["Current Liabilities"],
    "passivo_total": ["Total Liabilities Net Minority Interest"],
    "capital_proprio": ["Stockholders Equity", "Common Stock Equity"],
    "resultados_transitados": ["Retained Earnings"],
    "divida_lp": ["Long Term Debt And Capital Lease Obligation", "Long Term Debt"],
    "goodwill": ["Goodwill And Other Intangible Assets", "Goodwill"],
    "minoritarios": ["Minority Interest"],
    "investigacao": ["Research And Development"],
    "amortizacoes": ["Depreciation And Amortization", "Depreciation Amortization Depletion", "Reconciled Depreciation"],
    "receita_diferida": ["Current Deferred Revenue", "Current Deferred Liabilities"],
}

ANOS_MAX = 5
LIMIAR_COBERTURA_BOA = 5.0
LIMIAR_COBERTURA_FRACA = 3.0

# Moedas cotadas em subunidades (pence, cêntimos)
MOEDAS_SUBUNIDADE = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}

# Indústrias em que o balanço é o negócio: EBITDA, dívida líquida e FCF não se aplicam
INDUSTRIAS_FINANCEIRAS = ("Banks", "Insurance", "Mortgage Finance", "Capital Markets")


def moeda_principal(moeda: str | None) -> tuple[str | None, float]:
    """Devolve (moeda principal, fator) — por exemplo GBp → (GBP, 0.01)."""
    if moeda in MOEDAS_SUBUNIDADE:
        return MOEDAS_SUBUNIDADE[moeda]
    return moeda, 1.0


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _sem_fuso(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(idx))
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    return idx


def _serie_limpa(s: pd.Series | None) -> pd.Series | None:
    if s is None or len(s) == 0:
        return None
    s = pd.to_numeric(s, errors="coerce").copy()
    s.index = _sem_fuso(s.index)
    return s.dropna().sort_index()


def linha(df: pd.DataFrame | None, chave: str) -> pd.Series:
    """Devolve a série (ordenada por data crescente) do primeiro rótulo existente."""
    if df is None or getattr(df, "empty", True):
        return pd.Series(dtype=float)
    for nome in ROTULOS[chave]:
        if nome in df.index:
            s = pd.to_numeric(df.loc[nome], errors="coerce")
            s.index = _sem_fuso(s.index)
            s = s.dropna().sort_index()
            if not s.empty:
                return s
    return pd.Series(dtype=float)


def alinhar(s: pd.Series, datas: pd.DatetimeIndex) -> pd.Series:
    """Alinha uma série às datas de fim de exercício (tolerância de 20 dias)."""
    if s.empty:
        return pd.Series(np.nan, index=datas)
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(datas, method="nearest", tolerance=pd.Timedelta("20D"))


def ttm(df_trim: pd.DataFrame | None, chave: str) -> float:
    """Soma dos últimos 4 trimestres; NaN se não houver 4 trimestres consecutivos
    (empresas europeias com reporte semestral aparecem com buracos no Yahoo)."""
    s = linha(df_trim, chave)
    if len(s) < 4:
        return float("nan")
    ult = s.iloc[-4:]
    if (ult.index[-1] - ult.index[0]).days > 300:
        return float("nan")
    return float(ult.sum())


def ultimo(df: pd.DataFrame | None, chave: str) -> float:
    s = linha(df, chave)
    return float(s.iloc[-1]) if not s.empty else float("nan")


def cagr(serie: pd.Series) -> float:
    s = serie.dropna()
    if len(s) < 2 or s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return float("nan")
    anos = (s.index[-1] - s.index[0]).days / 365.25
    if anos <= 0:
        return float("nan")
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / anos) - 1)


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


# ---------------------------------------------------------------------------
# Splits: o preço do Yahoo vem ajustado a splits; as demonstrações podem ou
# não vir ajustadas. Detetamos pelo comportamento do número de ações.
# ---------------------------------------------------------------------------
def fatores_split(datas: pd.DatetimeIndex, splits: pd.Series | None, acoes: pd.Series):
    fatores = pd.Series(1.0, index=datas)
    avisos: list[str] = []
    if splits is None or len(splits) == 0:
        return fatores, avisos

    splits = splits[splits > 0].copy()
    splits.index = _sem_fuso(splits.index)
    acoes = acoes.dropna()

    for data_split, razao in splits.items():
        # só interessam splits posteriores ao primeiro exercício analisado
        antes = [d for d in datas if d < data_split]
        depois = [d for d in datas if d >= data_split]
        if not antes:
            continue
        if not depois:
            fatores[antes] *= razao
            avisos.append(
                f"Split de {razao:g}:1 em {data_split:%d/%m/%Y} posterior ao último exercício reportado. "
                "Assumi que as demonstrações ainda não estão ajustadas; confirma o EPS."
            )
            continue
        a_antes, a_depois = acoes.get(antes[-1], np.nan), acoes.get(depois[0], np.nan)
        if not (a_antes > 0 and a_depois > 0):
            avisos.append(
                f"Não consegui verificar se o split de {razao:g}:1 em {data_split:%d/%m/%Y} "
                "está refletido nas demonstrações; assumi que sim."
            )
            continue
        observado = math.log(a_depois / a_antes)
        dist_ajustado = abs(observado - 0.0)
        dist_nao_ajustado = abs(observado - math.log(razao))
        if dist_nao_ajustado < dist_ajustado:
            fatores[antes] *= razao
    return fatores, avisos


# ---------------------------------------------------------------------------
# Testes de tendência
# ---------------------------------------------------------------------------
def tendencia(serie: pd.Series, sentido: str = "subir", permite_caixa_liquida: bool = False) -> dict:
    """
    sentido: 'subir' ou 'descer'.
    Estado: 'cumpre' (todos os intervalos no sentido certo), 'parcial' (>=60%),
    'falha', ou 'nd' (menos de 3 pontos).
    """
    s = serie.dropna()
    if len(s) < 3:
        return {"estado": "nd", "detalhe": f"dados insuficientes ({len(s)} exercícios)"}
    if permite_caixa_liquida and (s <= 0).all():
        return {"estado": "cumpre", "detalhe": "caixa líquida em todos os exercícios"}
    dif = s.diff().dropna()
    certos = int((dif > 0).sum() if sentido == "subir" else (dif < 0).sum())
    total = len(dif)
    detalhe = f"{certos} de {total} variações anuais no sentido certo"
    if certos == total:
        return {"estado": "cumpre", "detalhe": detalhe}
    if certos / total >= 0.6:
        return {"estado": "parcial", "detalhe": detalhe}
    return {"estado": "falha", "detalhe": detalhe}


# ---------------------------------------------------------------------------
# Análise principal
# ---------------------------------------------------------------------------
def analisar(dados: dict) -> dict:
    avisos: list[str] = []
    info = dados.get("info") or {}
    inc, bal, cfl = dados.get("income"), dados.get("balance"), dados.get("cashflow")
    inc_q, cfl_q, bal_q = dados.get("income_q"), dados.get("cashflow_q"), dados.get("balance_q")

    receita = linha(inc, "receita")
    if receita.empty:
        raise ValueError("Sem demonstração de resultados anual para este ticker.")
    datas = receita.index[-ANOS_MAX:]
    n_anos = len(datas)
    if n_anos < ANOS_MAX:
        avisos.append(
            f"O Yahoo Finance devolveu apenas {n_anos} exercícios anuais (não 5). "
            f"As tendências usam {max(n_anos - 1, 0)} variações anuais."
        )

    tab = pd.DataFrame(index=datas)
    tab["receita"] = receita.reindex(datas)
    tab["lucro_bruto"] = alinhar(linha(inc, "lucro_bruto"), datas)
    tab["ebitda"] = alinhar(linha(inc, "ebitda"), datas)
    tab["ebit"] = alinhar(linha(inc, "ebit"), datas)
    tab["juros"] = alinhar(linha(inc, "juros"), datas).abs()
    tab["lucro_liquido"] = alinhar(linha(inc, "lucro_liquido"), datas)
    tab["taxa_imposto"] = alinhar(linha(inc, "taxa_imposto"), datas)
    tab["eps_reportado"] = alinhar(linha(inc, "eps"), datas)

    acoes = alinhar(linha(inc, "acoes_diluidas"), datas)
    tab["acoes_reportadas"] = acoes

    cfo = alinhar(linha(cfl, "cfo"), datas)
    tab["cfo"] = cfo
    fcf = alinhar(linha(cfl, "fcf"), datas)
    if fcf.isna().all():
        capex = alinhar(linha(cfl, "capex"), datas)
        fcf = cfo + capex  # capex vem negativo no Yahoo
        if not fcf.isna().all():
            avisos.append("FCF calculado como fluxo de caixa operacional + capex (linha direta não disponível).")
    tab["fcf"] = fcf
    tab["sbc"] = alinhar(linha(cfl, "sbc"), datas)
    tab["dividendos"] = alinhar(linha(cfl, "dividendos"), datas).abs()
    tab["recompras"] = alinhar(linha(cfl, "recompras"), datas).abs()
    tab["capex"] = alinhar(linha(cfl, "capex"), datas).abs()
    tab["amortizacoes"] = alinhar(linha(cfl, "amortizacoes"), datas).fillna(alinhar(linha(inc, "amortizacoes"), datas))
    tab["investigacao"] = alinhar(linha(inc, "investigacao"), datas)
    tab["receita_diferida"] = alinhar(linha(bal, "receita_diferida"), datas)

    # Dívida líquida: dívida total − caixa (consistente entre anos); a linha "Net Debt"
    # do Yahoo desaparece nos anos de caixa líquida, por isso só serve de recurso.
    nd_calc = alinhar(linha(bal, "divida_total"), datas) - alinhar(linha(bal, "caixa"), datas)
    nd = nd_calc.fillna(alinhar(linha(bal, "divida_liquida"), datas))
    tab["divida_liquida"] = nd

    for chave in ["ativo_total", "ativo_corrente", "passivo_corrente", "passivo_total", "capital_proprio",
                  "resultados_transitados", "divida_lp", "goodwill", "divida_total", "caixa"]:
        tab[chave] = alinhar(linha(bal, chave), datas)

    # Capital investido e capital próprio médios (início + fim do exercício) para ROIC e ROE
    cp_full, dt_full, cx_full = linha(bal, "capital_proprio"), linha(bal, "divida_total"), linha(bal, "caixa")
    if not cp_full.empty:
        base = pd.DataFrame({"cp": cp_full, "dt": dt_full, "cx": cx_full}).sort_index()
        capital = base["cp"] + base["dt"].fillna(0) - base["cx"].fillna(0)
        tab["capital_investido_medio"] = alinhar(capital.rolling(2, min_periods=1).mean(), datas)
        tab["capital_proprio_medio"] = alinhar(base["cp"].rolling(2, min_periods=1).mean(), datas)
    else:
        tab["capital_investido_medio"] = np.nan
        tab["capital_proprio_medio"] = np.nan

    # ---- ajuste a splits ---------------------------------------------------
    fatores, av = fatores_split(datas, dados.get("splits"), acoes)
    avisos += av
    tab["fator_split"] = fatores
    tab["acoes"] = acoes * fatores
    tab["eps"] = tab["eps_reportado"] / fatores
    sem_eps = tab["eps"].isna()
    if sem_eps.any():
        tab.loc[sem_eps, "eps"] = tab.loc[sem_eps, "lucro_liquido"] / tab.loc[sem_eps, "acoes"]

    # ---- derivadas -----------------------------------------------------------
    tab["crescimento_receita"] = tab["receita"].pct_change()
    tab["margem_bruta"] = tab["lucro_bruto"] / tab["receita"]
    tab["margem_ebitda"] = tab["ebitda"] / tab["receita"]
    tab["margem_ebit"] = tab["ebit"] / tab["receita"]
    tab["margem_fcf"] = tab["fcf"] / tab["receita"]
    tab["fcf_ajustado"] = tab["fcf"] - tab["sbc"].fillna(0)  # FCF tratando a remuneração em ações como custo
    tab["margem_fcf_ajustado"] = tab["fcf_ajustado"] / tab["receita"]
    tab["fcf_por_acao"] = tab["fcf"] / tab["acoes"]
    tab["cobertura_juros"] = np.where(tab["juros"] > 0, tab["ebit"] / tab["juros"], np.nan)
    tab["divida_liq_ebitda"] = np.where(tab["ebitda"] > 0, tab["divida_liquida"] / tab["ebitda"], np.nan)
    imposto = tab["taxa_imposto"].clip(0, 0.4).fillna(0.21)
    tab["roic"] = np.where(tab["capital_investido_medio"] > 0,
                           tab["ebit"] * (1 - imposto) / tab["capital_investido_medio"], np.nan)
    tab["roe"] = np.where(tab["capital_proprio_medio"] > 0, tab["lucro_liquido"] / tab["capital_proprio_medio"], np.nan)
    tab["conversao_fcf"] = np.where(tab["lucro_liquido"] > 0, tab["fcf"] / tab["lucro_liquido"], np.nan)
    tab["sbc_receita"] = tab["sbc"] / tab["receita"]
    tab["valor_contabilistico_acao"] = tab["capital_proprio"] / tab["acoes"]
    tab["nopat"] = tab["ebit"] * (1 - imposto)
    tab["capex_receita"] = tab["capex"] / tab["receita"]
    # capex de manutenção ≈ amortizações (o que se gasta para repor o que se desgasta); o resto é crescimento
    tab["capex_manutencao"] = np.minimum(tab["capex"], tab["amortizacoes"])
    tab["capex_crescimento"] = (tab["capex"] - tab["capex_manutencao"]).clip(lower=0)
    tab["fcf_normalizado"] = tab["cfo"] - tab["capex_manutencao"] - tab["sbc"].fillna(0)
    tab["margem_fcf_normalizado"] = tab["fcf_normalizado"] / tab["receita"]
    tab["margem_nopat"] = tab["nopat"] / tab["receita"]
    tab["investigacao_receita"] = tab["investigacao"] / tab["receita"]
    tab["receita_diferida_receita"] = tab["receita_diferida"] / tab["receita"]

    industria = info.get("industry") or ""
    modelo_financeiro = industria.startswith(INDUSTRIAS_FINANCEIRAS) or (
        info.get("sector") == "Financial Services" and tab["ebitda"].isna().all()
    )
    if modelo_financeiro:
        avisos.append(
            "Empresa financeira (banco, seguradora ou similar): EBITDA, dívida líquida, FCF e cobertura de juros "
            "não se aplicam. A qualidade usa o ROE e a avaliação usa o valor contabilístico (P/VC justificado)."
        )

    # ---- moeda e preços ------------------------------------------------------
    moeda_cot_original = info.get("currency")
    moeda_cot, escala = moeda_principal(moeda_cot_original)
    moeda_fin = info.get("financialCurrency") or moeda_cot
    fx = _serie_limpa(dados.get("fx"))  # unidades da moeda de cotação por 1 unidade da moeda financeira
    converte = bool(moeda_cot and moeda_fin and moeda_cot != moeda_fin)
    conversao_ok = True
    if converte:
        if fx is None or fx.empty:
            conversao_ok = False
            avisos.append(
                f"Moeda da cotação ({moeda_cot}) diferente da moeda das demonstrações ({moeda_fin}) e sem taxa de "
                "câmbio disponível: P/E histórico e avaliação não calculados para evitar um resultado errado."
            )
        else:
            avisos.append(
                f"Cotação em {moeda_cot} e demonstrações em {moeda_fin}: preços convertidos para {moeda_fin} "
                "à taxa de câmbio de cada data."
            )

    def cambio(data=None) -> float:
        if not converte:
            return 1.0
        if not conversao_ok:
            return float("nan")
        return float(fx.asof(data)) if data is not None else float(fx.iloc[-1])

    precos = _serie_limpa(dados.get("precos"))
    pe_valido = precos is not None and conversao_ok
    if precos is None:
        avisos.append("Sem histórico de preços: P/E histórico não calculado.")
    elif escala != 1.0:
        precos = precos * escala

    # ---- valores atuais, ações equivalentes e unidade cotada ---------------------
    preco_cot = info.get("currentPrice") or info.get("regularMarketPrice")
    preco_cot = preco_cot * escala if preco_cot else (float(precos.iloc[-1]) if precos is not None else None)
    fx_atual = cambio()
    preco_fin = preco_cot / fx_atual if preco_cot and not math.isnan(fx_atual) else None

    acoes_stmt = tab["acoes"].dropna()
    acoes_stmt_ult = float(acoes_stmt.iloc[-1]) if not acoes_stmt.empty else float("nan")
    mcap_cot = info.get("marketCap")
    if mcap_cot and preco_cot:
        acoes_eq = mcap_cot / preco_cot
    else:
        acoes_eq = acoes_stmt_ult
    fator_unidade = 1.0
    if acoes_eq and not math.isnan(acoes_eq) and acoes_stmt_ult > 0:
        f = acoes_eq / acoes_stmt_ult
        if not (0.8 <= f <= 1.25):  # fora disto não é recompra/diluição: é um ADR com rácio diferente de 1
            fator_unidade = f
            avisos.append(
                f"Cada unidade cotada corresponde a {1 / f:.3g} ações ordinárias (ADR ou classe de ações). "
                "Os valores por ação da avaliação estão expressos por unidade cotada."
            )
    mcap_fin = preco_fin * acoes_eq if preco_fin and acoes_eq and not math.isnan(acoes_eq) else None

    # ---- histórico de preço, capitalização, P/E e EV/EBIT no fim de cada exercício ---
    if pe_valido:
        preco_fim = []
        for d in tab.index:
            p = precos.asof(d)
            preco_fim.append(p / cambio(d) if not pd.isna(p) else np.nan)
        tab["preco_fim_exercicio"] = preco_fim
        tab["capitalizacao_fim"] = tab["preco_fim_exercicio"] * tab["acoes"] * fator_unidade
        eps_unidade = tab["eps"] / fator_unidade
        tab["pe_fim_exercicio"] = np.where(eps_unidade > 0, tab["preco_fim_exercicio"] / eps_unidade, np.nan)
        ev_fim = tab["capitalizacao_fim"] + tab["divida_liquida"]
        tab["ev_ebit_fim"] = np.where(tab["ebit"] > 0, ev_fim / tab["ebit"], np.nan)
        tab["rendimento_fcf_fim"] = tab["fcf_ajustado"] / tab["capitalizacao_fim"]
    else:
        for c in ["preco_fim_exercicio", "capitalizacao_fim", "pe_fim_exercicio", "ev_ebit_fim", "rendimento_fcf_fim"]:
            tab[c] = np.nan

    # ---- últimos 12 meses (4 trimestres consecutivos) ou último exercício -----------
    def ttm_ou_anual(df_q, chave, coluna):
        v = ttm(df_q, chave)
        if not math.isnan(v):
            return v, "12 meses"
        u = tab[coluna].dropna()
        return (float(u.iloc[-1]), "último exercício") if not u.empty else (float("nan"), "sem dados")

    atual = {}
    for chave, df_q, coluna in [("receita", inc_q, "receita"), ("ebit", inc_q, "ebit"), ("ebitda", inc_q, "ebitda"),
                                ("lucro_liquido", inc_q, "lucro_liquido"), ("juros", inc_q, "juros"),
                                ("fcf", cfl_q, "fcf"), ("sbc", cfl_q, "sbc")]:
        atual[chave], atual[f"{chave}_base"] = ttm_ou_anual(df_q, chave, coluna)
    atual["juros"] = abs(atual["juros"])
    atual["fcf_ajustado"] = atual["fcf"] - (0 if math.isnan(atual["sbc"]) else atual["sbc"])

    # balanço mais recente (trimestral, se existir)
    def balanco_recente(chave):
        v = ultimo(bal_q, chave)
        return v if not math.isnan(v) else ultimo(bal, chave)

    dt_r, cx_r = balanco_recente("divida_total"), balanco_recente("caixa")
    nd_atual = dt_r - cx_r if not (math.isnan(dt_r) or math.isnan(cx_r)) else balanco_recente("divida_liquida")
    if math.isnan(nd_atual):
        nd_atual = float(tab["divida_liquida"].dropna().iloc[-1]) if tab["divida_liquida"].notna().any() else float("nan")
    atual["divida_liquida"] = nd_atual
    minorit = balanco_recente("minoritarios")
    atual["minoritarios"] = 0.0 if math.isnan(minorit) else minorit
    atual["capital_proprio"] = balanco_recente("capital_proprio")

    pe_atual = None
    if mcap_fin and atual["lucro_liquido"] > 0:
        pe_atual = mcap_fin / atual["lucro_liquido"]
    ev_atual = mcap_fin + nd_atual + atual["minoritarios"] if mcap_fin and not math.isnan(nd_atual) else None
    ev_ebit_atual = ev_atual / atual["ebit"] if ev_atual and atual["ebit"] > 0 else None

    pes_hist = tab["pe_fim_exercicio"].dropna()
    pe_medio = float(pes_hist.mean()) if len(pes_hist) >= 3 else float("nan")
    pe_mediana = float(pes_hist.median()) if len(pes_hist) >= 3 else float("nan")
    if 0 < len(pes_hist) < 3:
        avisos.append("Menos de 3 exercícios com EPS positivo: média de P/E não fiável, não calculada.")

    # ---- cobertura de juros atual (TTM se possível) ----------------------------
    ebit_ttm, juros_ttm = ttm(inc_q, "ebit"), abs(ttm(inc_q, "juros"))
    if not math.isnan(ebit_ttm) and not math.isnan(juros_ttm) and juros_ttm > 0:
        cobertura_atual, base_cob = ebit_ttm / juros_ttm, "últimos 12 meses (4 trimestres)"
    else:
        ult = tab["cobertura_juros"].iloc[-1]
        ultimo_juros = tab["juros"].iloc[-1]
        if not pd.isna(ult):
            cobertura_atual, base_cob = float(ult), "último exercício anual"
        elif not pd.isna(ultimo_juros) and ultimo_juros == 0:
            cobertura_atual, base_cob = float("inf"), "sem juros reportados"
        else:
            cobertura_atual, base_cob = float("nan"), "sem dados"

    setor = info.get("sector")

    # ---- critérios ---------------------------------------------------------------
    criterios = []

    def add(nome, res, extra=""):
        detalhe = res["detalhe"] + (f" | {extra}" if extra else "")
        criterios.append({"criterio": nome, "estado": res["estado"], "detalhe": detalhe})

    def pp(serie):
        s = serie.dropna()
        return f"{(s.iloc[-1] - s.iloc[0]) * 100:+.1f} p.p. no período" if len(s) >= 2 else ""

    c = cagr(tab["receita"])
    add("Receitas crescentes", tendencia(tab["receita"], "subir"), f"CAGR {c*100:.1f}%" if not math.isnan(c) else "")
    add("Margem EBITDA crescente", tendencia(tab["margem_ebitda"], "subir"), pp(tab["margem_ebitda"]))
    add("Margem FCF crescente", tendencia(tab["margem_fcf"], "subir"), pp(tab["margem_fcf"]))
    c = cagr(tab["eps"])
    add("EPS crescente", tendencia(tab["eps"], "subir"), f"CAGR {c*100:.1f}%" if not math.isnan(c) else "")
    c = cagr(tab["fcf_por_acao"])
    add("FCF por ação crescente", tendencia(tab["fcf_por_acao"], "subir"), f"CAGR {c*100:.1f}%" if not math.isnan(c) else "")
    add("Dívida líquida decrescente", tendencia(tab["divida_liquida"], "descer", permite_caixa_liquida=True))

    if math.isnan(cobertura_atual):
        add("Cobertura de juros atual ≥ 5x", {"estado": "nd", "detalhe": "sem dados de EBIT/juros"})
    elif math.isinf(cobertura_atual):
        add("Cobertura de juros atual ≥ 5x", {"estado": "cumpre", "detalhe": f"sem despesa de juros reportada ({base_cob})"})
    else:
        est = "cumpre" if cobertura_atual >= LIMIAR_COBERTURA_BOA else "parcial" if cobertura_atual >= LIMIAR_COBERTURA_FRACA else "falha"
        add("Cobertura de juros atual ≥ 5x", {"estado": est, "detalhe": f"{cobertura_atual:.1f}x ({base_cob})"})

    if pe_atual and not math.isnan(pe_medio):
        desvio = pe_atual / pe_medio - 1
        est = "cumpre" if desvio <= 0.10 else "parcial" if desvio <= 0.30 else "falha"
        add("P/E atual não acima da média histórica",
            {"estado": est, "detalhe": f"atual {pe_atual:.1f}x vs média {pe_medio:.1f}x ({desvio*100:+.0f}%)"})
    else:
        add("P/E atual não acima da média histórica", {"estado": "nd", "detalhe": "P/E atual ou histórico indisponível"})

    pontos = {"cumpre": 1.0, "parcial": 0.5, "falha": 0.0}
    avaliados = [x for x in criterios if x["estado"] in pontos]
    score = sum(pontos[x["estado"]] for x in avaliados)

    # extras informativos
    dilui = tab["acoes"].dropna()
    extras = {
        "variacao_acoes_pct": float(dilui.iloc[-1] / dilui.iloc[0] - 1) if len(dilui) >= 2 and dilui.iloc[0] > 0 else float("nan"),
        "divida_liq_ebitda_atual": float(tab["divida_liq_ebitda"].iloc[-1]) if not pd.isna(tab["divida_liq_ebitda"].iloc[-1]) else float("nan"),
    }

    return {
        "ticker": dados.get("ticker"),
        "nome": info.get("longName") or info.get("shortName") or dados.get("ticker"),
        "setor": setor,
        "industria": industria,
        "pais": info.get("country"),
        "beta": info.get("beta"),
        "insiders": info.get("heldPercentInsiders"),
        "descricao": info.get("longBusinessSummary"),
        "modelo_financeiro": modelo_financeiro,
        "moeda": moeda_fin,
        "moeda_cotacao": moeda_cot,
        "moeda_cotacao_original": moeda_cot_original,
        "escala_cotacao": escala,
        "fx_atual": fx_atual,
        "preco_atual": preco_cot,
        "preco_fin": preco_fin,
        "capitalizacao": mcap_cot,
        "capitalizacao_fin": mcap_fin,
        "acoes_equivalentes": acoes_eq,
        "fator_unidade": fator_unidade,
        "ev_atual": ev_atual,
        "ev_ebit_atual": ev_ebit_atual,
        "atual": atual,
        "anual": tab,
        "criterios": criterios,
        "score": score,
        "score_max": len(avaliados),
        "pe_atual": pe_atual,
        "pe_medio": pe_medio,
        "pe_mediana": pe_mediana,
        "cobertura_atual": cobertura_atual,
        "crescimento_analistas": dados.get("crescimento_analistas") or {},
        "extras": extras,
        "avisos": avisos,
    }
