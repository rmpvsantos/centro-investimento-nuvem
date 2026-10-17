"""
Motor de cálculo da triagem de ações.

Recebe os dados em bruto (demonstrações financeiras, preços, splits, info) e
devolve as métricas e os critérios. Não depende do Streamlit nem do yfinance,
o que permite testar a lógica com dados simulados.
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
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "ebit": ["EBIT", "Operating Income"],
    "juros": ["Interest Expense", "Interest Expense Non Operating"],
    "lucro_liquido": ["Net Income Common Stockholders", "Net Income"],
    "eps": ["Diluted EPS", "Basic EPS"],
    "acoes_diluidas": ["Diluted Average Shares", "Basic Average Shares"],
    "fcf": ["Free Cash Flow"],
    "cfo": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure"],
    "divida_liquida": ["Net Debt"],
    "divida_total": ["Total Debt"],
    "caixa": ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"],
    "impostos": ["Tax Provision"],
    "lucro_antes_impostos": ["Pretax Income"],
    "ativos_totais": ["Total Assets"],
    "passivo_corrente": ["Current Liabilities", "Total Current Liabilities"],
    "capital_proprio": ["Total Equity Gross Minority Interest", "Stockholders Equity", "Common Stock Equity"],
    # usados na análise fundamental
    "capital_proprio_comum": ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"],
    "lucro_bruto": ["Gross Profit"],
    "ativo_corrente": ["Current Assets", "Total Current Assets"],
    "inventario": ["Inventory"],
    "sbc": ["Stock Based Compensation"],
    "recompras": ["Repurchase Of Capital Stock"],
    "dividendos": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
}

ANOS_MAX = 4  # o Yahoo gratuito devolve, no máximo, 4 exercícios anuais completos
# Valores de origem dos limiares de triagem. Podem ser alterados na aplicação (aba "Critérios de triagem"); nesse caso
# passam a vir de criterios_config.carregar() e são passados a analisar()/comparar_com_grupo() no parâmetro `limiares`.
LIMIAR_ROIC = 0.15
LIMIAR_ROCE = 0.15
LIMIAR_ND_EBITDA = 0.5  # abaixo deste rácio, a dívida líquida / EBITDA cumpre sempre
TAXA_IMPOSTO_OMISSAO = 0.21
LIMIAR_COBERTURA_BOA = 5.0
LIMIAR_COBERTURA_FRACA = 3.0
LIMIAR_PE_CUMPRE = 0.10   # desvio do P/E face à mediana do grupo até ao qual CUMPRE
LIMIAR_PE_PARCIAL = 0.30  # desvio até ao qual é PARCIAL (acima disto, FALHA)

LIMIARES_OMISSAO = {
    "roic": LIMIAR_ROIC, "roce": LIMIAR_ROCE, "nd_ebitda": LIMIAR_ND_EBITDA, "cobertura": LIMIAR_COBERTURA_BOA,
    "pe_cumpre": LIMIAR_PE_CUMPRE, "pe_parcial": LIMIAR_PE_PARCIAL,
}


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _sem_fuso(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(idx))
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    return idx


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
    """Soma dos últimos 4 trimestres; NaN se não houver 4 trimestres completos."""
    s = linha(df_trim, chave)
    if len(s) < 4:
        return float("nan")
    return float(s.iloc[-4:].sum())


def cagr(serie: pd.Series) -> float:
    s = serie.dropna()
    if len(s) < 2 or s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return float("nan")
    anos = (s.index[-1] - s.index[0]).days / 365.25
    if anos <= 0:
        return float("nan")
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / anos) - 1)


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
def analisar(dados: dict, limiares: dict | None = None) -> dict:
    """limiares: dicionário opcional para substituir um ou mais valores de LIMIARES_OMISSAO (roic, roce, nd_ebitda,
    cobertura; pe_cumpre e pe_parcial só são usados depois, em comparar_com_grupo)."""
    lim = {**LIMIARES_OMISSAO, **(limiares or {})}
    avisos: list[str] = []
    info = dados.get("info") or {}
    inc, bal, cfl = dados.get("income"), dados.get("balance"), dados.get("cashflow")
    inc_q, cfl_q = dados.get("income_q"), dados.get("cashflow_q")

    receita = linha(inc, "receita")
    if receita.empty:
        raise ValueError("Sem demonstração de resultados anual para este ticker.")
    datas = receita.index[-ANOS_MAX:]
    n_anos = len(datas)
    if n_anos < ANOS_MAX:
        avisos.append(
            f"O Yahoo Finance devolveu apenas {n_anos} exercícios anuais (pretendidos {ANOS_MAX}). "
            f"As tendências usam {max(n_anos - 1, 0)} variações anuais."
        )

    tab = pd.DataFrame(index=datas)
    tab["receita"] = receita.reindex(datas)
    tab["ebitda"] = alinhar(linha(inc, "ebitda"), datas)
    tab["ebit"] = alinhar(linha(inc, "ebit"), datas)
    tab["juros"] = alinhar(linha(inc, "juros"), datas).abs()
    tab["lucro_liquido"] = alinhar(linha(inc, "lucro_liquido"), datas)
    tab["eps_reportado"] = alinhar(linha(inc, "eps"), datas)

    acoes = alinhar(linha(inc, "acoes_diluidas"), datas)
    tab["acoes_reportadas"] = acoes

    fcf = alinhar(linha(cfl, "fcf"), datas)
    if fcf.isna().all():
        cfo = alinhar(linha(cfl, "cfo"), datas)
        capex = alinhar(linha(cfl, "capex"), datas)
        fcf = cfo + capex  # capex vem negativo no Yahoo
        if not fcf.isna().all():
            avisos.append("FCF calculado como fluxo de caixa operacional + capex (linha direta não disponível).")
    tab["fcf"] = fcf

    nd = alinhar(linha(bal, "divida_liquida"), datas)
    if nd.isna().all():
        nd = alinhar(linha(bal, "divida_total"), datas) - alinhar(linha(bal, "caixa"), datas)
        if not nd.isna().all():
            avisos.append("Dívida líquida calculada como dívida total menos caixa (linha direta não disponível).")
    tab["divida_liquida"] = nd

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
    tab["margem_ebitda"] = tab["ebitda"] / tab["receita"]
    tab["margem_fcf"] = tab["fcf"] / tab["receita"]
    tab["fcf_por_acao"] = tab["fcf"] / tab["acoes"]
    tab["cobertura_juros"] = np.where(tab["juros"] > 0, tab["ebit"] / tab["juros"], np.nan)
    tab["divida_liq_ebitda"] = np.where(tab["ebitda"] > 0, tab["divida_liquida"] / tab["ebitda"], np.nan)

    # ---- moeda e preços ------------------------------------------------------
    moeda_preco = info.get("currency")
    moeda_fin = info.get("financialCurrency")
    precos = dados.get("precos")
    pe_valido = precos is not None and len(precos) > 0
    if pe_valido:
        precos = precos.copy()
        precos.index = _sem_fuso(precos.index)
        precos = precos.dropna().sort_index()
        if moeda_preco == "GBp" and moeda_fin == "GBP":
            precos = precos / 100.0
            moeda_preco = "GBP"
        elif moeda_preco and moeda_fin and moeda_preco != moeda_fin:
            pe_valido = False
            avisos.append(
                f"Moeda do preço ({moeda_preco}) diferente da moeda das demonstrações ({moeda_fin}): "
                "P/E histórico não calculado para evitar um resultado errado."
            )
    else:
        avisos.append("Sem histórico de preços: P/E histórico não calculado.")

    if pe_valido:
        pes = []
        for d, eps in tab["eps"].items():
            preco = precos.asof(d)
            pes.append(preco / eps if (eps and eps > 0 and not pd.isna(preco)) else np.nan)
        tab["pe_fim_exercicio"] = pes
    else:
        tab["pe_fim_exercicio"] = np.nan

    # ---- valores atuais --------------------------------------------------------
    preco_atual = info.get("currentPrice") or info.get("regularMarketPrice")
    if not preco_atual and pe_valido:
        preco_atual = float(precos.iloc[-1])
    pe_atual = info.get("trailingPE")
    if (not pe_atual or (isinstance(pe_atual, float) and math.isnan(pe_atual))) and preco_atual and info.get("trailingEps"):
        eps_t = info["trailingEps"]
        pe_atual = preco_atual / eps_t if eps_t and eps_t > 0 else None
    if pe_atual is not None and (pe_atual <= 0 or (isinstance(pe_atual, float) and math.isnan(pe_atual))):
        pe_atual = None
    if moeda_preco and moeda_fin and moeda_preco != moeda_fin and moeda_preco != "GBp":
        pe_atual_comparavel = False
    else:
        pe_atual_comparavel = True

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
        ultimo = tab["cobertura_juros"].iloc[-1]
        ultimo_juros = tab["juros"].iloc[-1]
        if not pd.isna(ultimo):
            cobertura_atual, base_cob = float(ultimo), "último exercício anual"
        elif not pd.isna(ultimo_juros) and ultimo_juros == 0:
            cobertura_atual, base_cob = float("inf"), "sem juros reportados"
        else:
            cobertura_atual, base_cob = float("nan"), "sem dados"

    setor = info.get("sector")
    industria = info.get("industry")
    if setor == "Financial Services":
        avisos.append(
            "Setor financeiro: EBITDA, dívida líquida, cobertura de juros, ROIC e ROCE não são comparáveis com "
            "empresas não financeiras. Interpreta estes critérios com muita cautela."
        )

    # ---- ROIC e ROCE ---------------------------------------------------------------
    divida_total = alinhar(linha(bal, "divida_total"), datas)
    capital_proprio = alinhar(linha(bal, "capital_proprio"), datas)
    ativos = alinhar(linha(bal, "ativos_totais"), datas)
    passivo_corr = alinhar(linha(bal, "passivo_corrente"), datas)
    impostos = alinhar(linha(inc, "impostos"), datas)
    antes_impostos = alinhar(linha(inc, "lucro_antes_impostos"), datas)

    taxa = (impostos / antes_impostos).where(antes_impostos > 0).clip(0, 0.40)
    if taxa.notna().any():
        taxa = taxa.fillna(float(taxa.median()))
    else:
        taxa = pd.Series(TAXA_IMPOSTO_OMISSAO, index=datas)
        avisos.append(f"Taxa de imposto efetiva indisponível: usei {TAXA_IMPOSTO_OMISSAO:.0%} no cálculo do ROIC.")
    nopat = tab["ebit"] * (1 - taxa)
    capital_investido = (divida_total.fillna(0) + capital_proprio).where(capital_proprio.notna())
    ci_medio = ((capital_investido + capital_investido.shift(1)) / 2).fillna(capital_investido)
    tab["taxa_imposto"] = taxa
    tab["capital_investido"] = capital_investido
    tab["roic"] = (nopat / ci_medio).where(ci_medio > 0)
    capital_empregado = ativos - passivo_corr
    tab["roce"] = (tab["ebit"] / capital_empregado).where(capital_empregado > 0)

    # ---- métricas-chave (também usadas na comparação com o grupo) -------------------
    def variacao_total(s: pd.Series) -> float:
        s = s.dropna()
        if len(s) < 2 or s.iloc[0] <= 0:
            return float("nan")
        return float(s.iloc[-1] / s.iloc[0] - 1)

    def ultimo(s: pd.Series) -> float:
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else float("nan")

    def media(s: pd.Series) -> float:
        s = s.dropna()
        return float(s.mean()) if len(s) else float("nan")

    nd_ebitda_serie = pd.Series(tab["divida_liq_ebitda"].values, index=tab.index)
    chaves = {
        "receita_cagr": cagr(tab["receita"]),
        "eps_var_total": variacao_total(tab["eps"]),
        "fcf_cagr": cagr(tab["fcf"]),
        "ebitda_cagr": cagr(tab["ebitda"]),
        "roic_ultimo": ultimo(tab["roic"]),
        "roce_ultimo": ultimo(tab["roce"]),
        "nd_ebitda_atual": ultimo(nd_ebitda_serie),
        "cobertura_atual": cobertura_atual,
        "pe_atual": float(pe_atual) if pe_atual and pe_atual_comparavel else float("nan"),
    }
    periodo = f"{datas[0]:%Y}–{datas[-1]:%Y}"
    n_var = max(len(datas) - 1, 0)

    # ---- critérios -----------------------------------------------------------------
    criterios: list[dict] = []

    def add(nome, estado, detalhe, chave, tipo, limiar, melhor):
        criterios.append({
            "criterio": nome, "estado": estado, "detalhe": detalhe,
            "chave": chave, "valor": chaves.get(chave), "tipo": tipo, "limiar": limiar, "melhor": melhor,
        })

    def fmt_pct(v):
        return "n/d" if v is None or pd.isna(v) else f"{v * 100:.1f}%".replace(".", ",")

    def num1(v):
        return f"{v:.1f}".replace(".", ",")

    r = tendencia(tab["receita"], "subir")
    add("Receitas crescentes", r["estado"],
        f"CAGR {periodo} ({n_var} anos de crescimento): {fmt_pct(chaves['receita_cagr'])}. {r['detalhe']}",
        "receita_cagr", "pct", "crescer todos os anos", "maior")

    r = tendencia(tab["eps"], "subir")
    add("EPS crescente", r["estado"],
        f"Variação no período {periodo}: {fmt_pct(chaves['eps_var_total'])}. {r['detalhe']}",
        "eps_var_total", "pct", "crescer todos os anos", "maior")

    r = tendencia(tab["fcf"], "subir")
    add("FCF crescente", r["estado"],
        f"CAGR {periodo}: {fmt_pct(chaves['fcf_cagr'])}. {r['detalhe']}",
        "fcf_cagr", "pct", "crescer todos os anos", "maior")

    r = tendencia(tab["ebitda"], "subir")
    add("EBITDA crescente", r["estado"],
        f"CAGR {periodo}: {fmt_pct(chaves['ebitda_cagr'])}. {r['detalhe']}",
        "ebitda_cagr", "pct", "crescer todos os anos", "maior")

    def _lbl_pct(x):
        return str(round(x * 100, 1)).rstrip("0").rstrip(".").replace(".", ",")

    for nome_base, chave, serie, limiar in (
        ("ROIC", "roic_ultimo", tab["roic"], lim["roic"]),
        ("ROCE", "roce_ultimo", tab["roce"], lim["roce"]),
    ):
        rot_pct = _lbl_pct(limiar)
        nome = f"{nome_base} acima de {rot_pct}%"
        v = chaves[chave]
        if pd.isna(v):
            add(nome, "nd", "dados insuficientes no balanço ou na demonstração de resultados", chave, "pct", f"≥ {rot_pct}%", "maior")
        else:
            s = serie.dropna()
            det = (f"Último exercício {fmt_pct(v)}; média {len(s)} exercícios {fmt_pct(media(s))}; "
                   f"mínimo {fmt_pct(s.min())}, máximo {fmt_pct(s.max())}")
            add(nome, "cumpre" if v >= limiar else "falha", det, chave, "pct", f"≥ {rot_pct}%", "maior")

    r = tendencia(nd_ebitda_serie, "descer", permite_caixa_liquida=True)
    v = chaves["nd_ebitda_atual"]
    estado_nd, nota_nd = r["estado"], ""
    lnd = str(round(lim["nd_ebitda"], 2)).rstrip("0").rstrip(".").replace(".", ",")
    if not pd.isna(v) and v < lim["nd_ebitda"]:
        estado_nd = "cumpre"
        nota_nd = f"Abaixo de {lnd}x cumpre sempre. "
    add(f"Dívida líquida / EBITDA decrescente ou abaixo de {lnd}x", estado_nd,
        (f"Atual {num1(v)}x. " if not pd.isna(v) else "") + nota_nd + (r["detalhe"][:1].upper() + r["detalhe"][1:]) +
        (" (rácio calculado só nos exercícios com EBITDA positivo)" if nd_ebitda_serie.isna().any() else ""),
        "nd_ebitda_atual", "x", f"descer todos os anos ou < {lnd}x", "menor")

    v = cobertura_atual
    lc = str(round(lim["cobertura"], 2)).rstrip("0").rstrip(".").replace(".", ",")
    if pd.isna(v):
        add(f"Cobertura de juros superior a {lc}x", "nd", "sem dados de EBIT ou de juros", "cobertura_atual", "x", f"> {lc}x", "maior")
    elif math.isinf(v):
        add(f"Cobertura de juros superior a {lc}x", "cumpre", f"sem despesa de juros reportada ({base_cob})", "cobertura_atual", "x", f"> {lc}x", "maior")
    else:
        add(f"Cobertura de juros superior a {lc}x", "cumpre" if v > lim["cobertura"] else "falha",
            f"{num1(v)}x, base: {base_cob}", "cobertura_atual", "x", f"> {lc}x", "maior")

    lpc = _lbl_pct(lim["pe_cumpre"])
    add("P/E atual face ao P/E do setor", "nd", "sem grupo de comparação", "pe_atual", "x",
        f"atual ≤ mediana do grupo (+{lpc}%)", "menor")
    criterios[-1]["vs_grupo"] = True  # o estado é decidido em comparar_com_grupo

    score, score_max = pontuar(criterios)

    dilui = tab["acoes"].dropna()
    extras = {
        "variacao_acoes_pct": float(dilui.iloc[-1] / dilui.iloc[0] - 1) if len(dilui) >= 2 and dilui.iloc[0] > 0 else float("nan"),
        "divida_liq_ebitda_atual": chaves["nd_ebitda_atual"],
    }

    return {
        "ticker": dados.get("ticker"),
        "nome": info.get("longName") or info.get("shortName") or dados.get("ticker"),
        "setor": setor,
        "industria": industria,
        "setor_key": info.get("sectorKey"),
        "industria_key": info.get("industryKey"),
        "moeda": moeda_fin or moeda_preco,
        "preco_atual": preco_atual,
        "capitalizacao": info.get("marketCap"),
        "anual": tab,
        "periodo": periodo,
        "chaves": chaves,
        "criterios": criterios,
        "score": score,
        "score_max": score_max,
        "pe_atual": pe_atual if pe_atual_comparavel else None,
        "pe_medio": pe_medio,
        "pe_mediana": pe_mediana,
        "cobertura_atual": cobertura_atual,
        "extras": extras,
        "avisos": avisos,
    }


# ---------------------------------------------------------------------------
# Comparação com o grupo (mediana da indústria ou do setor)
# ---------------------------------------------------------------------------
MIN_EMPRESAS_MEDIANA = 3


def estatisticas_grupo(analises: list[dict]) -> dict:
    """Mediana de cada métrica-chave sobre as empresas comparáveis (valores inválidos são ignorados)."""
    saida: dict = {}
    chaves = set()
    for a in analises:
        chaves |= set(a["chaves"])
    for k in chaves:
        valores = []
        for a in analises:
            v = a["chaves"].get(k)
            if v is not None and not pd.isna(v) and not math.isinf(v):
                valores.append(float(v))
        saida[k] = {
            "mediana": float(np.median(valores)) if len(valores) >= MIN_EMPRESAS_MEDIANA else float("nan"),
            "n": len(valores),
        }
    return saida


def comparar_com_grupo(criterios: list[dict], estat: dict, pe_cumpre: float = LIMIAR_PE_CUMPRE,
                        pe_parcial: float = LIMIAR_PE_PARCIAL) -> list[dict]:
    """Junta a cada critério a mediana do grupo e se a empresa está melhor ou pior. pe_cumpre/pe_parcial: desvio do
    P/E face à mediana do grupo até ao qual CUMPRE e até ao qual é PARCIAL (acima disso, FALHA)."""
    out = []
    for c in criterios:
        c = dict(c)
        e = estat.get(c["chave"], {"mediana": float("nan"), "n": 0})
        med, valor = e["mediana"], c.get("valor")
        c["mediana_grupo"], c["n_grupo"] = med, e["n"]
        if c.get("vs_grupo"):
            if med is None or pd.isna(med) or med <= 0 or valor is None or pd.isna(valor):
                c["estado"], c["detalhe"] = "nd", "P/E atual da empresa ou do grupo indisponível"
            else:
                desvio = valor / med - 1
                c["estado"] = "cumpre" if desvio <= pe_cumpre else "parcial" if desvio <= pe_parcial else "falha"
                c["detalhe"] = (f"atual {valor:.1f}x vs mediana do grupo {med:.1f}x ({desvio * 100:+.0f}%)"
                                .replace(".", ","))
        if med is None or pd.isna(med) or valor is None or pd.isna(valor):
            c["face_grupo"] = "nd"
        elif isinstance(valor, float) and math.isinf(valor):
            c["face_grupo"] = "melhor" if c["melhor"] == "maior" else "pior"
        elif abs(valor - med) < 1e-12:
            c["face_grupo"] = "igual"
        else:
            melhor = valor > med if c["melhor"] == "maior" else valor < med
            c["face_grupo"] = "melhor" if melhor else "pior"
        out.append(c)
    return out


PONTOS = {"cumpre": 1.0, "parcial": 0.5, "falha": 0.0}


def pontuar(criterios: list[dict]) -> tuple[float, int]:
    """Pontuação (cumpre = 1, parcial = 0,5) e número de critérios avaliados (sem dados não conta)."""
    avaliados = [c for c in criterios if c["estado"] in PONTOS]
    return sum(PONTOS[c["estado"]] for c in avaliados), len(avaliados)


# ---------------------------------------------------------------------------
# Sugestão de triagem: merece ou não uma análise mais aprofundada
# ---------------------------------------------------------------------------
ELIMINATORIOS = {
    "receita_cagr": "crescimento da receita",
    "roic_ultimo": "ROIC",
    "nd_ebitda_atual": "dívida líquida / EBITDA",
    "cobertura_atual": "cobertura de juros",
}
LIMIAR_SUGESTAO_SIM = 0.75
LIMIAR_SUGESTAO_RESERVAS = 0.50
MIN_CRITERIOS_SUGESTAO = 7


def veredicto(criterios: list[dict], score: float, score_max: int) -> dict:
    """
    Sugestão de triagem (não é recomendação de compra):
      - "sim": pelo menos 75% dos pontos, nenhum critério eliminatório em FALHA e pelo menos 7 critérios avaliados;
      - "reservas": entre 50% e 75%, ou um critério eliminatório falhou, ou há poucos critérios avaliados;
      - "nao": abaixo de 50% dos pontos.
    Critérios eliminatórios: crescimento da receita, ROIC, dívida líquida / EBITDA e cobertura de juros
    (os que medem a qualidade do negócio e a solidez financeira). O P/E não bloqueia: um preço exigente
    é razão para analisar com cuidado, não para ignorar.
    """
    pct = score / score_max if score_max else 0.0
    falhas = [c["criterio"] for c in criterios if c["estado"] == "falha"]
    parciais = [c["criterio"] for c in criterios if c["estado"] == "parcial"]
    sem_dados = [c["criterio"] for c in criterios if c["estado"] == "nd"]
    elim = [ELIMINATORIOS[c["chave"]] for c in criterios if c["estado"] == "falha" and c.get("chave") in ELIMINATORIOS]
    pct_txt = f"{pct * 100:.0f}%"
    base = f"Cumpre {str(round(score, 1)).replace('.', ',')} de {score_max} pontos ({pct_txt})."

    if pct < LIMIAR_SUGESTAO_RESERVAS:
        nivel = "nao"
        titulo = "Não prioritária para análise aprofundada"
        resumo = f"{base} Fica abaixo de {int(LIMIAR_SUGESTAO_RESERVAS * 100)}% dos pontos possíveis."
    elif pct >= LIMIAR_SUGESTAO_SIM and not elim and score_max >= MIN_CRITERIOS_SUGESTAO:
        nivel = "sim"
        titulo = "Merece análise mais aprofundada"
        resumo = f"{base} Nenhum critério eliminatório falha (receita, ROIC, dívida líquida / EBITDA, cobertura de juros)."
    else:
        nivel = "reservas"
        titulo = "Análise aprofundada com reservas"
        motivos = []
        if elim:
            motivos.append("falha em critério eliminatório: " + ", ".join(elim))
        if pct < LIMIAR_SUGESTAO_SIM:
            motivos.append(f"pontuação abaixo de {int(LIMIAR_SUGESTAO_SIM * 100)}%")
        if score_max < MIN_CRITERIOS_SUGESTAO:
            motivos.append(f"só {score_max} critérios com dados")
        resumo = f"{base} Reservas: " + "; ".join(motivos) + "."

    pontos = []
    if falhas:
        pontos.append("Falham: " + "; ".join(falhas))
    if parciais:
        pontos.append("Parciais: " + "; ".join(parciais))
    if sem_dados:
        pontos.append("Sem dados: " + "; ".join(sem_dados))
    return {"nivel": nivel, "titulo": titulo, "resumo": resumo, "pontos": pontos, "eliminatorios_falhados": elim, "pct": pct}
