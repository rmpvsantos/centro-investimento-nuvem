"""
Uma pesquisa de oportunidades, definida por critérios que se podem gravar e repetir.

Os mesmos critérios servem a app (botão Procurar), as pesquisas guardadas e as pesquisas
automáticas (automatico.py, chamado pelo Agendador de Tarefas do Windows).

Formato dos critérios (dicionário, gravável em JSON):
    regioes      lista de códigos (us, ca, gb, de, ...)
    mcap         capitalização mínima, em milhões de USD
    setores      setores do Yahoo (vazio = todos)
    limite       número máximo de empresas a analisar
    ordem        ordem de análise quando o universo passa do limite (universo.ORDENS)
    yahoo        {chave do filtro: valor} dos filtros do Yahoo ligados (filtros.FILTROS_YAHOO)
    analise      {chave do filtro: valor} dos filtros calculados depois da análise (filtros.FILTROS_ANALISE)
    moat_forca   rótulo de filtros.FORCA_MOAT
    moat_tipos   lista de rótulos de filtros.TIPOS_MOAT
    limiar       fração mínima dos critérios que a empresa tem de cumprir (1.0 = todos; 0.8 = 80%)
    parametros   pressupostos da avaliação (campos de avaliacao.Parametros)

Desde 18/10/2026 há o FORMATO 2 (versao = 2, ver o fim do ficheiro e executar_v2): área, setor, indústria e um catálogo
de critérios, cada um com «limite» ou «mediana» do setor/indústria. É o que o Centro de Investimento usa; as pesquisas no
formato acima convertem-se sozinhas (normalizar).

Regiões, capitalização mínima e setores definem o universo e são sempre obrigatórios. Os restantes
filtros são os "critérios": com limiar abaixo de 100%, uma empresa entra se cumprir essa percentagem,
e o quadro mostra quais falhou.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict

import pandas as pd

from avaliacao import Parametros
from filtros import FILTROS_ANALISE, FILTROS_YAHOO, FORCA_MOAT, METRICAS, SETORES, TIPOS_MOAT
from universo import ORDENS, REGIOES, _nome_normalizado, analisar_lote, listar_universo, ordenar, paises_das_regioes

_YAHOO = {f[1]: f for f in FILTROS_YAHOO}
_ANALISE = {f[1]: f for f in FILTROS_ANALISE}
_SIMBOLO = {"gt": "≥", "gte": "≥", "lt": "≤", "btwn": "≤", "min": "≥", "max": "≤"}
LIMIAR_POR_OMISSAO = 0.8

def criterios_por_omissao() -> dict:
    return {
        "regioes": ["us", "ca"] + [r for r in REGIOES if r not in ("us", "ca")],
        "mcap": 5000, "setores": [], "limite": 200, "ordem": ORDENS[0],
        "yahoo": {f[1]: f[5] for f in FILTROS_YAHOO if f[9]}, "analise": {},
        "moat_forca": list(FORCA_MOAT)[0], "moat_tipos": [], "limiar": LIMIAR_POR_OMISSAO,
        "parametros": asdict(Parametros()),
    }


def parametros(crit: dict) -> Parametros:
    campos = set(Parametros.__dataclass_fields__)
    valores = {k: v for k, v in (crit.get("parametros") or {}).items() if k in campos}
    if "probabilidades" in valores:
        valores["probabilidades"] = tuple(valores["probabilidades"])
    return Parametros(**valores)


def condicoes_yahoo(crit: dict) -> list[tuple]:
    """[(campo do Yahoo, operador, valor)] para universo.listar_universo."""
    return [(_YAHOO[k][3], _YAHOO[k][4], v) for k, v in (crit.get("yahoo") or {}).items() if k in _YAHOO]


def filtros_depois(crit: dict) -> dict:
    """{coluna do quadro: (min | max, valor na escala da coluna)} para filtros.aplicar_filtros_finos."""
    out = {}
    for k, v in (crit.get("analise") or {}).items():
        if k in _ANALISE:
            _, _, _, coluna, tipo, *_resto, escala, _ajuda = _ANALISE[k]
            out[coluna] = (tipo, v * escala)
    return out


def _numero(v) -> str:
    return f"{v:g}".replace(".", ",")


def _curto(rotulo: str) -> str:
    """'ROIC mínimo (%)' → 'ROIC'; 'Crescimento do EPS no último ano, mínimo (%)' → 'Crescimento do EPS no último ano'."""
    return re.split(r",? (?:mínim|máxim)", rotulo)[0]


def _texto_criterio(filtro: tuple, operador: str, valor) -> str:
    """Como o critério aparece na lista 'Não cumpriu': 'ROIC ≥ 12', 'P/FCF ≤ 25'."""
    return f"{_curto(filtro[2])} {_SIMBOLO[operador]} {_numero(valor)}"


def _criterios_depois(crit: dict) -> list[tuple]:
    """[(texto, função(linha do quadro) → cumpre?)] dos critérios avaliados depois da análise."""
    out = []
    for k, v in (crit.get("analise") or {}).items():
        if k not in _ANALISE:
            continue
        f = _ANALISE[k]
        coluna, tipo, escala = f[3], f[4], f[9]
        limite = v * escala

        def cumpre(linha, coluna=coluna, tipo=tipo, limite=limite):
            x = linha.get(coluna)
            if x is None or pd.isna(x):
                return False  # sem o dado, não se pode dizer que cumpre
            return x >= limite if tipo == "min" else x <= limite

        out.append((_texto_criterio(f, tipo, v), cumpre))
    forca = FORCA_MOAT.get(crit.get("moat_forca"))
    if forca:
        out.append((f"Moat {crit['moat_forca'].lower()}", lambda linha, forca=forca: linha.get("Moat") in forca))
    tipos = [t for t in crit.get("moat_tipos") or [] if t in TIPOS_MOAT.values()]
    if tipos:
        out.append(("Tipo de moat: " + " ou ".join(tipos),
                    lambda linha, tipos=tipos: any(t in str(linha.get("Tipos de moat") or "").split("; ")
                                                   for t in tipos)))
    return out


def contagem(crit: dict) -> tuple[int, int]:
    """(número de critérios ativos, mínimo a cumprir com o limiar escolhido)."""
    if crit.get("versao") == VERSAO:
        return contagem_v2(crit)
    total = len(condicoes_yahoo(crit)) + len(_criterios_depois(crit))
    limiar = float(crit.get("limiar", 1.0))
    return total, (math.ceil(limiar * total - 1e-9) if total else 0)


def descricao(crit: dict) -> str:
    if crit.get("versao") == VERSAO:
        return descricao_v2(crit)
    regioes = crit.get("regioes") or []
    partes = [", ".join(REGIOES[r][0] for r in regioes if r in REGIOES),
              f"capitalização ≥ {crit.get('mcap', 0):,.0f} M USD".replace(",", ".")]
    if crit.get("setores"):
        partes.append(", ".join(SETORES.get(s, s) for s in crit["setores"]))
    partes += [f"{_YAHOO[k][2]}: {_numero(v)}" for k, v in (crit.get("yahoo") or {}).items() if k in _YAHOO]
    partes += [f"{_ANALISE[k][2]}: {_numero(v)}" for k, v in (crit.get("analise") or {}).items() if k in _ANALISE]
    if FORCA_MOAT.get(crit.get("moat_forca")):
        partes.append(f"moat: {crit['moat_forca'].lower()}")
    if crit.get("moat_tipos"):
        partes.append("tipo de moat: " + ", ".join(crit["moat_tipos"]))
    total, minimo = contagem(crit)
    if total and minimo < total:
        partes.append(f"cumprir pelo menos {minimo} de {total} critérios ({float(crit.get('limiar', 1)):.0%})")
    return "; ".join(partes)


def executar(crit: dict, progresso=None) -> dict:
    """Corre a pesquisa completa. Devolve dict com df (empresas que passam), falhas, e contagens
    (universo, analisadas) para explicar o funil."""
    return executar_v2(crit, progresso)  # formato 2 (as pesquisas do formato 1 convertem-se em normalizar)
    regioes = [r for r in crit.get("regioes") or [] if r in REGIOES]
    if not regioes:
        raise ValueError("A pesquisa não tem regiões.")
    mcap, setores = crit.get("mcap", 5000) * 1e6, crit.get("setores") or []
    criterios_y = [(k, _YAHOO[k], v) for k, v in (crit.get("yahoo") or {}).items() if k in _YAHOO]
    depois = _criterios_depois(crit)
    total, minimo = contagem(crit)
    folga = total - minimo  # quantos critérios uma empresa pode falhar

    if folga == 0:
        # todos obrigatórios: o Yahoo filtra tudo de uma vez (mais rápido)
        uni = listar_universo(regioes, mcap, condicoes_yahoo(crit), setores, progresso=progresso)
        falhou_y = {}
    else:
        # uma consulta por critério para saber, empresa a empresa, quais falha. A chave é o nome normalizado,
        # porque entre consultas o Yahoo pode devolver listagens diferentes da mesma empresa.
        uni = listar_universo(regioes, mcap, [], setores, progresso=progresso)
        chaves = uni["Empresa"].map(_nome_normalizado) if not uni.empty else pd.Series(dtype=str)
        falhas_empresa = {c: [] for c in chaves}
        for i, (k, f, v) in enumerate(criterios_y, start=1):
            if progresso:
                progresso(i / max(len(criterios_y), 1), f"Critério {i} de {len(criterios_y)}: {_curto(f[2])}")
            cumprem = listar_universo(regioes, mcap, [(f[3], f[4], v)], setores)
            ok = set(cumprem["Empresa"].map(_nome_normalizado)) if not cumprem.empty else set()
            for c in falhas_empresa:
                if c not in ok:
                    falhas_empresa[c].append(_texto_criterio(f, f[4], v))
        if not uni.empty:
            uni = uni.assign(_falhou=chaves.map(falhas_empresa))
            # os critérios depois da análise ainda podem ser cumpridos: só sai quem já não chega ao mínimo
            uni = uni[uni["_falhou"].map(len) <= folga]
        falhou_y = dict(zip(uni["Ticker"], uni["_falhou"])) if not uni.empty else {}

    uni = ordenar(uni, crit.get("ordem") or ORDENS[0])
    limite = int(crit.get("limite") or 200)
    tickers = uni["Ticker"].tolist()[:limite] if not uni.empty else []
    df, falhas = (analisar_lote(tickers, parametros(crit), progresso=progresso, paises=paises_das_regioes(regioes))
                  if tickers else (pd.DataFrame(), []))
    analisadas = len(df)

    if not df.empty and total:
        nao_cumpriu = []
        for _, linha in df.iterrows():
            lista = list(falhou_y.get(linha["Ticker"], []))
            lista += [texto for texto, cumpre in depois if not cumpre(linha)]
            nao_cumpriu.append(lista)
        n_falhas = pd.Series([len(x) for x in nao_cumpriu], index=df.index)
        df = df.assign(**{"Critérios cumpridos": (total - n_falhas) / total,
                          "Critérios": [f"{total - n} de {total}" for n in n_falhas],
                          "Não cumpriu": ["; ".join(x) for x in nao_cumpriu]})
        df = df[n_falhas <= folga]
        # quem cumpre tudo primeiro, dentro de cada classificação
        df = df.sort_values(["ordem", "Critérios cumpridos", "Preço (nota)"], ascending=[True, False, False])
    if not df.empty:
        df = df.reset_index(drop=True)
        df.attrs["descricao"] = descricao(crit)
    return {"df": df, "falhas": falhas, "universo": len(uni), "a_analisar": len(tickers), "analisadas": analisadas,
            "filtros_depois": bool(depois), "criterios": total, "minimo": minimo, "descricao": descricao(crit)}


# ---------------------------------------------------------------------------------------------------------------------
# Pesquisas no formato 2 (pedido do Rui a 18/10/2026)
#   área (EUA e Canadá | Europa, todos os países) → setor (ou todos) → indústria (ou todas) → critérios escolhidos por
#   ele, cada um com o «limite meu» (ex.: ROIC ≥ 15%) ou «melhor que a mediana» do setor/indústria → percentagem de
#   critérios a cumprir. Cada métrica conta uma só vez. Os valores vêm todos da análise do motor (linha_resumo).
# ---------------------------------------------------------------------------------------------------------------------
VERSAO = 2
AREAS = {"eua_canada": ("EUA e Canadá", ["us", "ca"]), "europa": ("Europa (todos os países)", [r for r in REGIOES if r not in ("us", "ca")])}
GRUPOS_CRITERIOS = ["Operações", "Rentabilidade e eficiência", "Endividamento", "Avaliação", "Retorno ao acionista", "Outros"]
# chave, rótulo, grupo, coluna do quadro (motor.linha_resumo), sentido («acima» = maior é melhor), formato, limite por
# omissão (na unidade mostrada: % nas percentagens), passo, ajuda
CATALOGO = [
    ("cresc_receitas", "Crescimento das receitas (anual composto)", "Operações", "Crescimento receitas (CAGR)", "acima", "pct", 8, 1,
     "Crescimento anual composto das receitas nos exercícios disponíveis (4 a 5)."),
    ("cresc_eps", "Crescimento do EPS (anual composto)", "Operações", "Crescimento EPS (CAGR)", "acima", "pct", 8, 1,
     "Crescimento anual composto do lucro por ação (sem valor se o primeiro ou o último ano tiver prejuízo)."),
    ("cresc_fcf", "Crescimento do FCF (anual composto)", "Operações", "Crescimento FCF (CAGR)", "acima", "pct", 8, 1,
     "Crescimento anual composto do fluxo de caixa livre."),
    ("cresc_ebitda", "EBITDA crescente (crescimento anual composto)", "Operações", "Crescimento EBITDA (CAGR)", "acima", "pct", 5, 1,
     "Crescimento anual composto do EBITDA: positivo = EBITDA a crescer."),
    ("capex", "Capex (% das receitas)", "Operações", "Capex (% receitas)", "abaixo", "pct", 10, 1,
     "Investimento em percentagem das receitas (mediana dos exercícios). Menos = negócio que precisa de menos capital."),
    ("margem_bruta", "Margem bruta", "Rentabilidade e eficiência", "Margem bruta mediana", "acima", "pct", 40, 1, "Mediana dos exercícios."),
    ("margem_liquida", "Margem líquida (profit margin)", "Rentabilidade e eficiência", "Margem líquida mediana", "acima", "pct", 10, 1,
     "Lucro líquido em percentagem das receitas (mediana)."),
    ("margem_fcf", "Margem FCF", "Rentabilidade e eficiência", "Margem FCF mediana", "acima", "pct", 10, 1,
     "FCF depois da remuneração em ações, em percentagem das receitas (mediana)."),
    ("margem_ebitda", "Margem EBITDA", "Rentabilidade e eficiência", "Margem EBITDA mediana", "acima", "pct", 20, 1, "Mediana dos exercícios."),
    ("rotacao_ativo", "Rotação do ativo (asset turnover)", "Rentabilidade e eficiência", "Rotação do ativo", "acima", "x", 0.6, 0.1,
     "Receitas ÷ ativo total (mediana): quanto vende cada euro de ativo."),
    ("roe", "ROE", "Rentabilidade e eficiência", "ROE mediano", "acima", "pct", 15, 1, "Lucro ÷ capital próprio médio (mediana)."),
    ("roa", "ROA", "Rentabilidade e eficiência", "ROA mediano", "acima", "pct", 7, 1, "Lucro ÷ ativo total (mediana)."),
    ("roic", "ROIC", "Rentabilidade e eficiência", "ROIC mediano", "acima", "pct", 12, 1,
     "Resultado operacional depois de impostos ÷ capital investido médio (mediana)."),
    ("roce", "ROCE", "Rentabilidade e eficiência", "ROCE mediano", "acima", "pct", 15, 1, "EBIT ÷ (ativo − passivo corrente) (mediana)."),
    ("caixa_liquida", "Caixa líquida (% da capitalização)", "Rentabilidade e eficiência", "Caixa líquida (% capitalização)", "acima", "pct", 0, 1,
     "(Caixa − dívida) ÷ capitalização. Positivo = mais caixa do que dívida."),
    ("nd_capital", "Dívida líquida / capital próprio", "Endividamento", "Dívida líquida/Capital próprio", "abaixo", "x", 0.5, 0.1,
     "Negativo = caixa líquida."),
    ("nd_ebitda", "Dívida líquida / EBITDA", "Endividamento", "Dívida líquida/EBITDA", "abaixo", "x", 2, 0.5, "Anos de EBITDA para pagar a dívida líquida."),
    ("nd_fcf", "Dívida líquida / FCF", "Endividamento", "Dívida líquida/FCF", "abaixo", "x", 3, 0.5, "Anos de FCF para pagar a dívida líquida."),
    ("liquidez_corrente", "Current ratio", "Endividamento", "Liquidez corrente", "acima", "x", 1.2, 0.1, "Ativo corrente ÷ passivo corrente."),
    ("quick_ratio", "Quick ratio", "Endividamento", "Quick ratio", "acima", "x", 1, 0.1, "(Ativo corrente − inventários) ÷ passivo corrente (Yahoo)."),
    ("cobertura", "Cobertura de juros (interest coverage)", "Endividamento", "Cobertura de juros", "acima", "x", 8, 1,
     "EBIT ÷ juros. Sem dívida = 999."),
    ("pe", "P/E", "Avaliação", "P/E", "abaixo", "x", 25, 1, "Preço ÷ lucro dos últimos 12 meses."),
    ("pe_futuro", "P/E futuro (forward)", "Avaliação", "P/E futuro", "abaixo", "x", 22, 1, "Preço ÷ lucro previsto pelos analistas (Yahoo)."),
    ("peg", "PEG", "Avaliação", "PEG", "abaixo", "x", 1.5, 0.1, "P/E ÷ crescimento esperado do lucro (Yahoo)."),
    ("p_fcf", "P/FCF", "Avaliação", "P/FCF", "abaixo", "x", 25, 1, "Capitalização ÷ FCF dos últimos 12 meses."),
    ("p_b", "P/B (preço / valor contabilístico)", "Avaliação", "P/B", "abaixo", "x", 5, 0.5, "Yahoo."),
    ("ev_ebitda", "EV/EBITDA", "Avaliação", "EV/EBITDA", "abaixo", "x", 15, 1, "Valor da empresa (com dívida) ÷ EBITDA dos últimos 12 meses."),
    ("payout", "Payout ratio", "Retorno ao acionista", "Payout", "abaixo", "pct", 60, 5,
     "Dividendos ÷ lucro do último exercício. Abaixo de 60% deixa margem para manter o dividendo."),
    ("buyback_yield", "Buyback yield", "Retorno ao acionista", "Recompras (% capitalização)", "acima", "pct", 1, 0.5,
     "Recompras do último exercício ÷ capitalização."),
    ("sbc", "SBC (% das receitas)", "Retorno ao acionista", "SBC (% receitas)", "abaixo", "pct", 3, 0.5,
     "Remuneração em ações em percentagem das receitas (mediana): dilui os acionistas."),
    ("dividend_yield", "Dividend yield", "Retorno ao acionista", "Dividend yield", "acima", "pct", 2, 0.5,
     "Dividendos do último exercício ÷ capitalização."),
    ("moat", "Força do moat (1 = estreito ou largo; 2 = só largo)", "Outros", "Moat (nível)", "acima", "num", 1, 1,
     "Moat medido pelos números no motor: 2 = largo, 1 = estreito, 0 = sem moat. Só com limite."),
    ("altman", "Altman Z-Score", "Outros", "Altman Z", "acima", "num", 3, 0.1, "Risco de falência: acima de 2,99 zona segura."),
    ("piotroski", "Piotroski F-Score", "Outros", "Piotroski", "acima", "num", 6, 1, "Nove testes de melhoria no último ano (0 a 9)."),
]
_CAT = {c[0]: c for c in CATALOGO}
SO_POSITIVOS = {"pe", "pe_futuro", "peg", "p_fcf", "p_b", "ev_ebitda"}  # múltiplos com prejuízo não contam nem entram na mediana
SO_LIMITE = {"moat"}                       # critérios que não se comparam com a mediana (classe, não número)
MIN_EMPRESAS_OMISSAO = 15                  # abaixo disto, a mediana da indústria recua para o setor (mesma área)
MIN_VALORES_METRICA = 5                    # menos valores do que isto num grupo: sem mediana (o critério não conta)
GRUPOS_MEDIANA = {"regiao": "a mesma área da pesquisa", "eua_europa": "EUA, Canadá e Europa juntos"}
MODOS = ("limite", "mediana")


def escala(chave: str) -> float:
    """Valor escrito pelo Rui → valor da coluna: 15 (%) → 0,15; 2 (x) → 2."""
    return 0.01 if _CAT[chave][5] == "pct" else 1.0


def _num_ok(chave: str, v) -> bool:
    if v is None or isinstance(v, bool):
        return False
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False
    if math.isnan(v) or math.isinf(v):
        return False
    return not (chave in SO_POSITIVOS and v <= 0)


def area_das_regioes(regioes: list[str]) -> str:
    return "eua_canada" if set(regioes or []) & {"us", "ca"} else "europa"


def normalizar(crit: dict | None) -> dict:
    """Critérios no formato 2. Os do formato 1 (as pesquisas guardadas até 18/10/2026) convertem-se: cada filtro com
    equivalente passa a critério «limite» com o mesmo valor; os que não têm equivalente ficam em «nao_convertidos»."""
    crit = dict(crit or {})
    if crit.get("versao") == VERSAO:
        return crit
    mapa_y = {"roic": "roic", "margem_bruta": "margem_bruta", "margem_liquida": "margem_liquida", "margem_ebitda": "margem_ebitda",
              "divida": "nd_ebitda", "cobertura": "cobertura", "liquidez": "liquidez_corrente", "altman": "altman",
              "cresc_receitas": "cresc_receitas", "cresc_eps": "cresc_eps", "cresc_fcf": "cresc_fcf", "pe": "pe", "peg": "peg",
              "ev_ebitda": "ev_ebitda"}
    mapa_a = {"p_fcf": "p_fcf", "recompras": "buyback_yield"}
    criterios, fora = {}, []
    for k, v in (crit.get("yahoo") or {}).items():
        (criterios.__setitem__(mapa_y[k], {"modo": "limite", "valor": v}) if k in mapa_y else fora.append(k))
    for k, v in (crit.get("analise") or {}).items():
        (criterios.__setitem__(mapa_a[k], {"modo": "limite", "valor": v}) if k in mapa_a else fora.append(k))
    forca = crit.get("moat_forca") or ""
    if forca.startswith("Só largo"):
        criterios["moat"] = {"modo": "limite", "valor": 2}
    elif forca.startswith("Estreito"):
        criterios["moat"] = {"modo": "limite", "valor": 1}
    if crit.get("moat_tipos"):
        fora.append("moat_tipos")
    setores = crit.get("setores") or []
    return {"versao": VERSAO, "area": area_das_regioes(crit.get("regioes")), "setor": setores[0] if len(setores) == 1 else "",
            "industria": "", "mcap": crit.get("mcap", 5000), "limite": crit.get("limite", 200), "ordem": crit.get("ordem") or ORDENS[0],
            "criterios": criterios, "limiar": crit.get("limiar", LIMIAR_POR_OMISSAO), "grupo_mediana": "regiao",
            "min_empresas": MIN_EMPRESAS_OMISSAO, "parametros": crit.get("parametros") or asdict(Parametros()),
            "nao_convertidos": fora}


def criterios_ativos(crit: dict) -> list[tuple[str, str, float | None]]:
    """[(chave, modo, limite na escala da coluna ou None na mediana)] dos critérios escolhidos e válidos."""
    out = []
    for k, c in (normalizar(crit).get("criterios") or {}).items():
        if k not in _CAT:
            continue
        modo = c.get("modo") if c.get("modo") in MODOS else "limite"
        if modo == "mediana" and k in SO_LIMITE:
            modo = "limite"
        if modo == "limite":
            if not _num_ok("x", c.get("valor")):
                continue
            out.append((k, modo, float(c["valor"]) * escala(k)))
        else:
            out.append((k, modo, None))
    return out


def _fmt(chave: str, v: float) -> str:
    f = _CAT[chave][5]
    if f == "pct":
        return f"{v * 100:.1f}%".replace(".", ",")
    if f == "x":
        return f"{v:.1f}x".replace(".", ",")
    return f"{v:g}".replace(".", ",")


def texto_criterio_v2(chave: str, modo: str, limite: float | None, mediana: float | None = None) -> str:
    rot, sent = _CAT[chave][1], _CAT[chave][4]
    sinal = "≥" if sent == "acima" else "≤"
    if modo == "limite":
        return f"{rot} {sinal} {_fmt(chave, limite)}"
    return f"{rot} {'>' if sent == 'acima' else '<'} mediana" + (f" ({_fmt(chave, mediana)})" if mediana is not None else "")


def cumpre(chave: str, modo: str, valor, limite: float | None, mediana: float | None) -> bool | None:
    """True / False; None quando o critério é pela mediana e o grupo não tem mediana (não conta). Sem valor = não cumpre.
    Limite: igual ao limite cumpre (≥ / ≤). Mediana: tem de ser melhor (estritamente)."""
    sent = _CAT[chave][4]
    ref = limite if modo == "limite" else mediana
    if ref is None:
        return None
    if not _num_ok(chave, valor):
        return False
    v = float(valor)
    if modo == "limite":
        return v >= ref if sent == "acima" else v <= ref
    return v > ref if sent == "acima" else v < ref


def calcular_grupos(pares: pd.DataFrame, coluna_grupo: str) -> dict:
    """{grupo: {"n": empresas, "medianas": {chave: {"mediana", "n"}}}}: a mediana de cada critério do catálogo no grupo
    (setor ou indústria) de cada empresa. Com menos de MIN_VALORES_METRICA valores, a mediana fica None."""
    out = {}
    if pares is None or pares.empty or coluna_grupo not in pares:
        return out
    for g, sub in pares.groupby(coluna_grupo):
        meds = {}
        for k, *_r in CATALOGO:
            if k in SO_LIMITE:
                continue
            col = _CAT[k][3]
            vals = [float(v) for v in (sub[col] if col in sub else []) if _num_ok(k, v)]
            meds[k] = {"mediana": float(pd.Series(vals).median()) if len(vals) >= MIN_VALORES_METRICA else None, "n": len(vals)}
        out[str(g)] = {"n": int(len(sub)), "medianas": meds}
    return out


def avaliar(valores: dict, grupo: dict | None, ativos: list[tuple], limiar: float) -> dict:
    """Uma empresa: cada critério (cumpre / falha / sem mediana), quantos contam, quantos cumpre e se passa."""
    linhas = []
    for k, modo, lim in ativos:
        med = ((grupo or {}).get("medianas") or {}).get(k, {}).get("mediana") if modo == "mediana" else None
        ok = cumpre(k, modo, valores.get(k), lim, med)
        linhas.append({"chave": k, "modo": modo, "cumpre": ok, "texto": texto_criterio_v2(k, modo, lim, med)})
    contam = [x for x in linhas if x["cumpre"] is not None]
    total, n_ok = len(contam), sum(1 for x in contam if x["cumpre"])
    minimo = math.ceil(limiar * total - 1e-9) if total else 0
    return {"criterios": linhas, "total": total, "cumpre": n_ok, "minimo": minimo, "passa": total > 0 and n_ok >= minimo}


def contagem_v2(crit: dict) -> tuple[int, int]:
    total = len(criterios_ativos(crit))
    return total, (math.ceil(float(normalizar(crit).get("limiar", 1)) * total - 1e-9) if total else 0)


def descricao_v2(crit: dict) -> str:
    c = normalizar(crit)
    partes = [AREAS.get(c.get("area"), AREAS["eua_canada"])[0],
              SETORES.get(c.get("setor"), c.get("setor")) if c.get("setor") else "todos os setores",
              (c.get("industria") or "").replace("—", " - ") or "todas as indústrias",
              f"capitalização ≥ {c.get('mcap', 0):,.0f} M USD".replace(",", ".")]
    partes += [texto_criterio_v2(k, m, lim) for k, m, lim in criterios_ativos(c)]
    total, minimo = contagem_v2(c)
    if total:
        partes.append(f"cumprir pelo menos {minimo} de {total} critérios ({float(c.get('limiar', 1)):.0%})")
    if any(m == "mediana" for _k, m, _l in criterios_ativos(c)):
        partes.append(f"mediana: {GRUPOS_MEDIANA.get(c.get('grupo_mediana'), GRUPOS_MEDIANA['regiao'])}")
    return "; ".join(partes)


def mesma_industria(a, b) -> bool:
    """O screener escreve «Software—Infrastructure»; a análise, «Software - Infrastructure»."""
    norm = lambda s: re.sub(r"\s+", " ", str(s or "").replace("—", " - ")).strip().lower()  # noqa: E731
    return bool(a) and norm(a) == norm(b)


def _valores(linha) -> dict:
    out = {}
    for k, *_r in CATALOGO:
        v = linha.get(_CAT[k][3])
        out[k] = float(v) if _num_ok("x", v) else None
    return out


def executar_v2(crit: dict, progresso=None) -> dict:
    """
    1. Universo: as empresas da área (EUA e Canadá, ou Europa), do setor e da indústria escolhidos, com a capitalização
       mínima; analisadas pelo motor pela ordem escolhida (as maiores primeiro, por omissão), até ao limite.
    2. Medianas (só se algum critério for «melhor que a mediana»): cada empresa compara-se com o seu grupo — a indústria
       escolhida; com a indústria em «todas», o setor de cada empresa. Com o grupo «EUA, Canadá e Europa juntos», as
       empresas da outra área entram também no cálculo (não nos resultados). Com menos de min_empresas analisadas na
       indústria, recua-se para o setor, na mesma área, e diz-se porquê.
    3. Cada critério cumpre ou falha; os critérios pela mediana num grupo sem mediana não contam. Passa quem cumpre a
       percentagem escolhida.
    Devolve o mesmo que executar (df das que passam, contagens) e «resultado»: os valores de todas as analisadas e as
    medianas de todos os grupos, para a página recalcular na hora quando se mudam os critérios.
    """
    from datetime import datetime

    c = normalizar(crit)
    area = c.get("area") if c.get("area") in AREAS else "eua_canada"
    regs = AREAS[area][1]
    setor, industria = c.get("setor") or "", c.get("industria") or ""
    mcap, limite = float(c.get("mcap") or 5000) * 1e6, int(c.get("limite") or 200)
    p = parametros(c)
    ativos = criterios_ativos(c)
    usa_mediana = any(m == "mediana" for _k, m, _l in ativos)
    grupo_med = c.get("grupo_mediana") if c.get("grupo_mediana") in GRUPOS_MEDIANA else "regiao"
    min_emp = int(c.get("min_empresas") or MIN_EMPRESAS_OMISSAO)

    def analisar(regioes, setores_, industrias_):
        uni = listar_universo(regioes, mcap, [], setores_, progresso=progresso, industrias=industrias_)
        uni = ordenar(uni, c.get("ordem") or ORDENS[0])
        tickers = uni["Ticker"].tolist()[:limite] if not uni.empty else []
        df_, falhas_ = (analisar_lote(tickers, p, progresso=progresso, paises=paises_das_regioes(regioes))
                        if tickers else (pd.DataFrame(), []))
        return df_, falhas_, len(uni)

    filtro = ([setor] if setor else [], [industria] if industria else None)
    df, falhas, n_universo = analisar(regs, *filtro)
    pares, nota = df, None
    coluna_grupo = "Indústria" if industria else "Setor"
    nivel = "industria" if industria else ("setor" if setor else "setor de cada empresa")
    if usa_mediana:
        if grupo_med == "eua_europa":
            outra = AREAS["europa" if area == "eua_canada" else "eua_canada"][1]
            df_o, _f, _n = analisar(outra, *filtro)
            pares = pd.concat([df, df_o], ignore_index=True) if not df_o.empty else df
        if industria and len(pares) < min_emp:
            setor_ind = setor or (str(df["Setor"].iloc[0]) if not df.empty else "")
            setor_yahoo = setor or next((k for k, v in SETORES.items() if v == setor_ind), setor_ind)
            if setor_yahoo:
                regs_pares = regs if grupo_med == "regiao" else regs + AREAS["europa" if area == "eua_canada" else "eua_canada"][1]
                p_setor, _f, _n = analisar(regs_pares, [setor_yahoo], None)
                if len(p_setor) > len(pares):
                    nota = (f"só {len(pares)} empresas analisadas na indústria ({industria.replace('—', ' - ')}), abaixo do mínimo de "
                            f"{min_emp}: as medianas são as do setor {SETORES.get(setor_yahoo, setor_yahoo)}")
                    pares, coluna_grupo, nivel = p_setor, "Setor", "setor (recuo: poucas empresas na indústria)"
    # as medianas calculam-se sempre (a página deixa mudar um critério para «mediana» sem nova procura)
    grupos = calcular_grupos(pares, coluna_grupo)
    if usa_mediana:
        pequenos = [g for g, x in grupos.items() if x["n"] < min_emp]
        if pequenos and not nota:
            nota = f"grupos com menos de {min_emp} empresas (medianas pouco fiáveis): {', '.join(pequenos[:6])}"

    limiar = float(c.get("limiar", LIMIAR_POR_OMISSAO))
    empresas, passa, cumpridos, textos, nao = [], [], [], [], []
    for _, linha in df.iterrows():
        g = str(linha.get(coluna_grupo)) if coluna_grupo in linha else None
        vals = _valores(linha)
        a = avaliar(vals, grupos.get(g), ativos, limiar)
        passa.append(a["passa"])
        cumpridos.append(a["cumpre"] / a["total"] if a["total"] else None)
        textos.append(f"{a['cumpre']} de {a['total']}")
        nao.append("; ".join(x["texto"] for x in a["criterios"] if x["cumpre"] is False))
        empresas.append({"Ticker": linha["Ticker"], "Empresa": linha.get("Empresa"), "País": linha.get("País"), "Setor": linha.get("Setor"),
                         "Indústria": linha.get("Indústria"), "grupo": g, "Classificação": linha.get("Classificação"),
                         "Moeda": linha.get("Moeda"), "Preço atual": linha.get("Preço atual"), "valores": vals})
    out = df.copy()
    if not out.empty:
        out = out.assign(**{"Critérios cumpridos": cumpridos, "Critérios": textos, "Não cumpriu": nao})
        out = out[pd.Series(passa, index=out.index)] if ativos else out
        out = out.sort_values(["ordem", "Critérios cumpridos", "Preço (nota)"], ascending=[True, False, False]).reset_index(drop=True)
        if not out.empty:
            out.attrs["descricao"] = descricao_v2(c)
    total, minimo = contagem_v2(c)
    resultado = {"quando": datetime.now().isoformat(timespec="minutes"), "area": area, "area_nome": AREAS[area][0], "setor": setor,
                 "industria": industria, "mcap": c.get("mcap"), "limite": limite, "nivel": nivel, "coluna_grupo": coluna_grupo,
                 "grupo_mediana": grupo_med, "min_empresas": min_emp, "nota": nota, "n_universo": n_universo, "n_analisadas": len(df),
                 "n_pares": len(pares) if usa_mediana else None, "grupos": grupos, "empresas": empresas, "falhas": len(falhas)}
    return {"df": out, "falhas": falhas, "universo": n_universo, "a_analisar": min(limite, n_universo), "analisadas": len(df),
            "filtros_depois": False, "criterios": total, "minimo": minimo, "descricao": descricao_v2(c), "resultado": resultado}
