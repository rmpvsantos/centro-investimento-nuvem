"""
Filtros da descoberta e leitura do quadro de resultados, definidos num só sítio.

- FILTROS_YAHOO: pré-filtros aplicados pelo screener do Yahoo antes da análise (rápidos,
  mas excluem as empresas a quem o Yahoo não tem o valor).
- METRICAS: o que cada coluna do quadro mede e os limiares verde / amarelo / vermelho,
  usados nas cores, nas explicações e nos filtros finos.
- VISTAS: grupos de colunas, para não mostrar 30 colunas de uma vez.
"""

from __future__ import annotations

import math

import pandas as pd

# ---------------------------------------------------------------------------
# Setores (nomes do Yahoo → português)
# ---------------------------------------------------------------------------
SETORES = {
    "Technology": "Tecnologia",
    "Healthcare": "Saúde",
    "Financial Services": "Serviços financeiros",
    "Consumer Cyclical": "Consumo cíclico",
    "Consumer Defensive": "Consumo defensivo",
    "Industrials": "Indústria",
    "Communication Services": "Comunicações",
    "Energy": "Energia",
    "Basic Materials": "Materiais de base",
    "Real Estate": "Imobiliário",
    "Utilities": "Serviços públicos (utilities)",
}

# ---------------------------------------------------------------------------
# Pré-filtros do screener do Yahoo (unidades confirmadas: percentagens em %)
# ---------------------------------------------------------------------------
GRUPOS_FILTROS = ["Rentabilidade", "Solidez", "Operações", "Preço", "Rendimentos"]

FILTROS_YAHOO = [
    # grupo, chave, rótulo, campo do Yahoo, operador, padrão, mínimo, máximo, passo, ativo por omissão, ajuda
    ("Rentabilidade", "roic", "ROIC mínimo (%)", "returnontotalcapital.lasttwelvemonths", "gt", 12, 0, 40, 1, True,
     "Retorno sobre o capital total. Acima de 12-15% sugere vantagem competitiva."),
    ("Rentabilidade", "margem_bruta", "Margem bruta mínima (%)", "grossprofitmargin.lasttwelvemonths", "gt", 30, 0, 90, 5, False,
     "Margem bruta alta indica poder de fixação de preços. Não usar em retalho ou distribuição."),
    ("Rentabilidade", "margem_liquida", "Margem líquida mínima (%)", "netincomemargin.lasttwelvemonths", "gt", 8, -20, 50, 1, False,
     "Lucro líquido em percentagem das receitas."),
    ("Rentabilidade", "margem_ebitda", "Margem EBITDA mínima (%)", "ebitdamargin.lasttwelvemonths", "gt", 20, 0, 70, 1, False,
     "Resultado operacional antes de amortizações em percentagem das receitas: rentabilidade das operações "
     "antes da estrutura de capital e do investimento."),
    ("Solidez", "divida", "Dívida líquida / EBITDA máxima", "netdebtebitda.lasttwelvemonths", "lt", 2.5, 0, 6, 0.5, True,
     "Anos de EBITDA para pagar a dívida líquida. Até 1x folgado, acima de 3x arriscado. Empresas com caixa "
     "líquida passam."),
    ("Solidez", "cobertura", "Cobertura de juros mínima (EBIT / juros)", "ebitinterestexpense.lasttwelvemonths", "gt", 5, 1, 30, 1, False,
     "Quantas vezes o resultado operacional paga os juros. Abaixo de 3x é frágil. Exclui empresas sem dívida "
     "(o Yahoo não tem o valor)."),
    ("Solidez", "liquidez", "Liquidez corrente mínima (current ratio)", "currentratio.lasttwelvemonths", "gt", 1.0, 0, 3, 0.1, False,
     "Ativo corrente / passivo corrente. Abaixo de 1 pode haver aperto de tesouraria."),
    ("Solidez", "altman", "Altman Z-Score mínimo", "altmanzscoreusingtheaveragestockinformationforaperiod.lasttwelvemonths",
     "gt", 1.8, 0, 6, 0.1, False, "Risco de falência: acima de 2,99 zona segura, abaixo de 1,81 perigo."),
    ("Operações", "cresc_receitas", "Crescimento das receitas no último ano, mínimo (%)",
     "totalrevenues1yrgrowth.lasttwelvemonths", "gt", 5, -30, 50, 1, False,
     "Exclui negócios em contração. Um só ano: não confundir com tendência."),
    ("Operações", "cresc_eps", "Crescimento do EPS no último ano, mínimo (%)", "epsgrowth.lasttwelvemonths", "gt", 5, -50, 100, 5, False,
     "Crescimento do lucro por ação nos últimos 12 meses."),
    ("Operações", "cresc_fcf", "Crescimento do FCF no último ano, mínimo (%)",
     "leveredfreecashflow1yrgrowth.lasttwelvemonths", "gt", 5, -50, 100, 5, False,
     "Crescimento do fluxo de caixa livre nos últimos 12 meses. Muito volátil num só ano: usar com limites largos."),
    ("Preço", "pe", "P/E máximo", "peratio.lasttwelvemonths", "btwn", 30, 5, 80, 1, False,
     "Preço / lucro dos últimos 12 meses. Exclui empresas com prejuízo."),
    ("Preço", "peg", "PEG máximo", "pegratio_5y", "btwn", 1.5, 0.2, 4, 0.1, False,
     "P/E dividido pelo crescimento esperado do lucro (5 anos). Abaixo de 1 o preço é baixo face ao crescimento."),
    ("Preço", "ev_ebitda", "EV/EBITDA máximo", "lastclosetevebitda.lasttwelvemonths", "btwn", 15, 3, 40, 1, True,
     "Valor da empresa (com dívida) / EBITDA. Abaixo de 10x é barato para um negócio médio; acima de 20x exige "
     "crescimento elevado."),
    ("Rendimentos", "anos_dividendo", "Anos seguidos a aumentar o dividendo, mínimo",
     "consecutive_years_of_dividend_growth_count", "gte", 5, 1, 40, 1, False,
     "Historial de aumento do dividendo: disciplina de capital e confiança da gestão na caixa futura. Exclui "
     "empresas sem dividendo."),
]

# Filtros que o Yahoo não tem: calculados com os dados da análise e aplicados logo a seguir, na mesma pesquisa.
# grupo, chave, rótulo, coluna do quadro, 'min' | 'max', padrão, mínimo, máximo, passo, escala, ajuda
FILTROS_ANALISE = [
    ("Preço", "p_fcf", "P/FCF máximo", "P/FCF", "max", 25, 5, 80, 1, 1,
     "Capitalização / fluxo de caixa livre. Abaixo de 15x é barato; acima de 30x o preço depende de crescimento."),
    ("Rendimentos", "recompras", "Recompras mínimas (% da capitalização por ano)", "Recompras (% capitalização)",
     "min", 1, 0, 10, 0.5, 0.01,
     "Valor gasto em recompras de ações no último exercício, em percentagem da capitalização. Recompras "
     "a preços razoáveis aumentam a parte de cada acionista."),
]

# Tipos de moat (as fontes medidas em moat.py). O "retorno acima do custo de capital" não é um tipo:
# é a prova de que existe moat, e entra pela força (largo/estreito).
TIPOS_MOAT = {
    "Poder de fixação de preços": "Marca e poder de preço",
    "Receitas recorrentes e custos de mudança": "Custos de mudança e receitas recorrentes",
    "Escala e alavancagem operacional": "Escala",
    "Eficiência de capital": "Eficiência de capital",
    "Reinvestimento a retornos altos": "Reinvestimento rentável",
}
FORCA_MOAT = {"Qualquer (sem filtro)": None, "Estreito ou largo": ["Moat estreito", "Moat largo"],
              "Só largo": ["Moat largo"]}


# ---------------------------------------------------------------------------
# Métricas do quadro: o que medem e como se leem
# ---------------------------------------------------------------------------
# formato: pct (percentagem), pp (pontos percentuais), x (múltiplo), nota (0-100), num (número), int
METRICAS = {
    "Qualidade": dict(fmt="nota", bom=70, mau=50, maior=True,
                      ajuda="Qualidade do negócio (0-100): ROIC, margens, conversão em caixa, balanço, diluição, Piotroski."),
    "Preço (nota)": dict(fmt="nota", bom=60, mau=40, maior=True,
                         ajuda="Atratividade do preço (0-100): desconto face ao valor, DCF inverso, rendibilidade do FCF, "
                               "EV/EBIT e P/E face à própria história."),
    "Qualitativa": dict(fmt="nota", bom=60, mau=40, maior=True,
                        ajuda="A tua avaliação qualitativa (0-100). Vazio = ainda não preenchida."),
    "Confiança": dict(fmt="pct0", bom=0.8, mau=0.6, maior=True,
                      ajuda="Percentagem dos critérios que foi possível avaliar com os dados do Yahoo."),
    "Alertas graves": dict(fmt="int", bom=0, mau=0, maior=False,
                           ajuda="Sinais de armadilha de valor: receitas em queda, dívida excessiva, FCF negativo, "
                                 "diluição forte, Altman em perigo. Ver o detalhe na análise individual."),
    "Desconto": dict(fmt="pct0", bom=0.20, mau=0.0, maior=True,
                     ajuda="1 − preço / valor esperado. Positivo: o preço está abaixo do valor estimado. "
                           "20% ou mais dá margem de segurança."),
    "Crescimento exigido vs base": dict(fmt="pp", bom=-0.03, mau=0.03, maior=False,
                                        ajuda="Crescimento que o preço pressupõe (DCF inverso) menos o do cenário base, em "
                                              "pontos percentuais. Negativo: o mercado espera menos do que a empresa "
                                              "tem mostrado (onde pode estar o desconto). Positivo: paga-se por "
                                              "crescimento extra."),
    "ROIC mediano": dict(fmt="pct", bom=0.15, mau=0.10, maior=True,
                         ajuda="Retorno sobre o capital investido (mediana dos exercícios). Acima de 15% sugere vantagem "
                               "competitiva; abaixo de 10% o crescimento pode destruir valor."),
    "ROE mediano": dict(fmt="pct", bom=0.15, mau=0.10, maior=True,
                        ajuda="Retorno sobre o capital próprio (mediana). Principal medida para bancos e seguradoras."),
    "Margem bruta mediana": dict(fmt="pct", bom=0.40, mau=0.20, maior=True,
                                 ajuda="Margem bruta alta e estável = poder de fixação de preços. Baixa é normal em "
                                       "retalho e distribuição."),
    "Margem FCF mediana": dict(fmt="pct", bom=0.10, mau=0.05, maior=True,
                               ajuda="Fluxo de caixa livre em percentagem das receitas, descontada a remuneração em ações."),
    "Crescimento receitas (CAGR)": dict(fmt="pct", bom=0.08, mau=0.0, maior=True,
                                        ajuda="Crescimento anual composto das receitas nos exercícios disponíveis (4 a 5)."),
    "Diluição anual": dict(fmt="pct", bom=0.0, mau=0.02, maior=False,
                           ajuda="Variação anual do número de ações. Negativo = recompras (bom); acima de 2% ao ano os "
                                 "acionistas estão a ser diluídos."),
    "Rendibilidade FCF": dict(fmt="pct", bom=0.05, mau=0.03, maior=True,
                              ajuda="FCF ajustado / capitalização: o retorno em caixa ao preço atual. Acima de 5% é "
                                    "atrativo; abaixo de 3% o preço depende de crescimento futuro."),
    "EV/EBIT": dict(fmt="x", bom=15, mau=25, maior=False,
                    ajuda="Valor da empresa (com dívida) / resultado operacional. Até 15x barato para um negócio médio; "
                          "acima de 25x exige crescimento elevado."),
    "P/E": dict(fmt="x", bom=18, mau=30, maior=False,
                ajuda="Preço / lucro dos últimos 12 meses. Até 18x barato; acima de 30x caro, salvo crescimento forte."),
    "Dívida líquida/EBITDA": dict(fmt="x", bom=1, mau=3, maior=False,
                                  ajuda="Anos de EBITDA para pagar a dívida líquida. Negativo = caixa líquida. Até 1x "
                                        "folgado; acima de 3x arriscado."),
    "Cobertura de juros": dict(fmt="x", bom=8, mau=3, maior=True,
                               ajuda="EBIT / juros. Acima de 8x confortável; abaixo de 3x frágil."),
    "Piotroski": dict(fmt="num0", bom=7, mau=4, maior=True,
                      ajuda="Piotroski F-Score (0 a 9): nove testes de melhoria no último ano (lucro, caixa, dívida, "
                            "liquidez, margens, eficiência, sem emissão de ações). 7-9 forte; 4-6 neutro; 0-3 fraco."),
    "Altman Z": dict(fmt="num", bom=2.99, mau=1.81, maior=True,
                     ajuda="Altman Z-Score: risco de falência. Acima de 2,99 zona segura; 1,81 a 2,99 zona cinzenta; "
                           "abaixo de 1,81 perigo. Pensado para industriais; em serviços é indicativo."),
    "P/FCF": dict(fmt="x", bom=15, mau=30, maior=False,
                  ajuda="Capitalização / fluxo de caixa livre dos últimos 12 meses. Até 15x barato; acima de 30x o "
                        "preço depende de crescimento futuro."),
    "Recompras (% capitalização)": dict(fmt="pct", bom=0.02, mau=0.0, maior=True,
                                        ajuda="Recompras de ações no último exercício em percentagem da capitalização "
                                              "atual. Somadas ao dividendo, é o que a empresa devolve aos acionistas."),
    "Critérios cumpridos": dict(fmt="pct0", bom=1.0, mau=0.8, maior=True,
                                ajuda="Percentagem dos critérios da pesquisa que a empresa cumpre. Abaixo de 100%, "
                                      "a coluna 'Não cumpriu' diz quais falhou (um filtro do Yahoo também falha "
                                      "quando o Yahoo não tem o valor)."),
    "Moat (nota)": dict(fmt="nota", bom=70, mau=45, maior=True,
                        ajuda="Força da vantagem competitiva medida pelos números (0-100): retorno acima do custo de "
                              "capital, poder de fixação de preços, escala, receitas recorrentes, eficiência de capital "
                              "e reinvestimento a retornos altos."),
}

# colunas de texto com cor própria
CORES_TEXTO = {
    "Moat largo": "color:#3ecf8e;font-weight:600", "Moat estreito": "color:#f2b134;font-weight:600",
    "Sem moat evidente": "color:#ef5b5b", "A reforçar-se": "color:#3ecf8e;font-weight:600",
    "A erodir": "color:#ef5b5b;font-weight:600", "Estável": "color:#a9b6cc",
}

VISTAS = {
    "Resumo": ["Classificação", "Critérios cumpridos", "Não cumpriu", "País", "Setor", "Qualidade", "Preço (nota)",
               "Moat", "Desconto", "Moeda", "Preço atual", "Valor esperado", "Preço de entrada", "Alertas graves",
               "Confiança"],
    "Qualidade e moat": ["Classificação", "Qualidade", "Moat", "Moat (nota)", "Tipos de moat", "Tendência do moat",
                         "ROIC mediano", "ROE mediano", "Margem bruta mediana", "Margem FCF mediana",
                         "Crescimento receitas (CAGR)", "Diluição anual", "Recompras (% capitalização)", "Piotroski",
                         "Qualitativa"],
    "Valorização": ["Classificação", "Preço (nota)", "Moeda", "Preço atual", "Valor pessimista", "Valor esperado",
                    "Valor otimista", "Preço de entrada", "Desconto", "Crescimento exigido vs base",
                    "Rendibilidade FCF", "P/FCF", "EV/EBIT", "P/E"],
    "Solidez financeira": ["Classificação", "Dívida líquida/EBITDA", "Cobertura de juros", "Altman Z", "Piotroski",
                           "Alertas graves"],
}
COLUNAS_FIXAS = ["Ticker", "Empresa"]

VERDE = "background-color:rgba(62,207,142,.16);color:#7ee2b2"
AMARELO = "background-color:rgba(242,177,52,.14);color:#f5c96e"
VERMELHO = "background-color:rgba(239,91,91,.16);color:#f39a9a"


def _vazio(v) -> bool:
    return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def cor(coluna: str, v) -> str:
    m = METRICAS.get(coluna)
    if m is None or _vazio(v):
        return ""
    if m["maior"]:
        return VERDE if v >= m["bom"] else VERMELHO if v < m["mau"] else AMARELO
    return VERDE if v <= m["bom"] else VERMELHO if v > m["mau"] else AMARELO


def _fmt_limiar(m: dict, v: float) -> str:
    f = m["fmt"]
    if f in ("pct", "pct0"):
        return f"{v * 100:.0f}%".replace(".", ",")
    if f == "pp":
        return f"{v * 100:+.0f} p.p."
    if f == "x":
        return f"{v:g}x".replace(".", ",")
    return f"{v:g}".replace(".", ",")


def legenda() -> pd.DataFrame:
    """Tabela explicativa: o que cada coluna mede e os limiares de cada cor."""
    linhas = []
    for nome, m in METRICAS.items():
        b, u = _fmt_limiar(m, m["bom"]), _fmt_limiar(m, m["mau"])
        if m["bom"] == m["mau"]:
            verde, amarelo, vermelho = f"{b}", "—", f"acima de {u}" if not m["maior"] else f"abaixo de {u}"
        elif m["maior"]:
            verde, amarelo, vermelho = f"≥ {b}", f"{u} a {b}", f"< {u}"
        else:
            verde, amarelo, vermelho = f"≤ {b}", f"{b} a {u}", f"> {u}"
        linhas.append({"Coluna": nome, "Verde (bom)": verde, "Amarelo": amarelo, "Vermelho (mau)": vermelho,
                       "O que mede": m["ajuda"]})
    return pd.DataFrame(linhas)


def ajuda_coluna(coluna: str) -> str | None:
    m = METRICAS.get(coluna)
    if m is None:
        return None
    b, u = _fmt_limiar(m, m["bom"]), _fmt_limiar(m, m["mau"])
    if m["bom"] == m["mau"]:
        limiar = f"Verde: {b}."
    elif m["maior"]:
        limiar = f"Verde ≥ {b}; amarelo {u} a {b}; vermelho < {u}."
    else:
        limiar = f"Verde ≤ {b}; amarelo {b} a {u}; vermelho > {u}."
    return f"{m['ajuda']} {limiar}"


# ---------------------------------------------------------------------------
# Filtros finos sobre os resultados (instantâneos, não fazem pedidos)
# ---------------------------------------------------------------------------
# coluna, rótulo, tipo ('min' ou 'max'), padrão, mínimo, máximo, passo, escala (valor do slider × escala = valor da coluna)
FILTROS_FINOS = [
    ("Qualidade", "Qualidade mínima", "min", 70, 0, 100, 5, 1),
    ("Preço (nota)", "Nota de preço mínima", "min", 60, 0, 100, 5, 1),
    ("Moat (nota)", "Nota do moat mínima", "min", 60, 0, 100, 5, 1),
    ("Desconto", "Desconto mínimo face ao valor (%)", "min", 10, -50, 60, 5, 0.01),
    ("Crescimento exigido vs base", "Crescimento exigido vs base, máximo (p.p.)", "max", 0, -20, 20, 1, 0.01),
    ("ROIC mediano", "ROIC mediano mínimo (%)", "min", 15, 0, 60, 1, 0.01),
    ("Margem FCF mediana", "Margem FCF mínima (%)", "min", 10, -10, 50, 1, 0.01),
    ("Rendibilidade FCF", "Rendibilidade do FCF mínima (%)", "min", 4, 0, 15, 0.5, 0.01),
    ("Crescimento receitas (CAGR)", "Crescimento das receitas (CAGR) mínimo (%)", "min", 5, -20, 40, 1, 0.01),
    ("Diluição anual", "Diluição anual máxima (%)", "max", 1, -10, 10, 0.5, 0.01),
    ("Dívida líquida/EBITDA", "Dívida líquida / EBITDA máxima", "max", 2, -2, 6, 0.5, 1),
    ("Piotroski", "Piotroski mínimo", "min", 6, 0, 9, 1, 1),
    ("Altman Z", "Altman Z mínimo", "min", 3.0, 0, 10, 0.1, 1),
    ("EV/EBIT", "EV/EBIT máximo", "max", 20, 5, 60, 1, 1),
    ("P/FCF", "P/FCF máximo", "max", 25, 5, 80, 1, 1),
    ("Recompras (% capitalização)", "Recompras mínimas (% da capitalização)", "min", 1, 0, 10, 0.5, 0.01),
    ("Confiança", "Confiança mínima dos dados (%)", "min", 80, 0, 100, 10, 0.01),
]


def filtrar_moat(df: pd.DataFrame, forca: list[str] | None, tipos: list[str] | None) -> pd.DataFrame:
    """forca: classificações aceites (ex.: ['Moat largo']); tipos: fontes que têm de ser fortes (basta uma)."""
    out = df
    if forca and "Moat" in out:
        out = out[out["Moat"].isin(forca)]
    if tipos and "Tipos de moat" in out:
        out = out[out["Tipos de moat"].fillna("").map(lambda s: any(t in s.split("; ") for t in tipos))]
    return out


def aplicar_filtros_finos(df: pd.DataFrame, ativos: dict, excluir_sem_dados: bool) -> pd.DataFrame:
    """ativos = {coluna: (tipo, valor_na_escala_da_coluna)}."""
    out = df
    for coluna, (tipo, valor) in ativos.items():
        if coluna not in out:
            continue
        serie = out[coluna]
        passa = serie >= valor if tipo == "min" else serie <= valor
        if not excluir_sem_dados:
            passa = passa | serie.isna()
        out = out[passa]
    return out
