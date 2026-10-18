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
    mediana      (opcional) comparar com a mediana da indústria: {"ativo", "setor", "industria", "grupo",
                 "min_empresas", "metricas"} — ver executar_com_mediana. Cada métrica escolhida é um critério
                 («melhor do que a mediana»), que conta para o limiar como os outros.

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

# ---------------------------------------------------------------------------
# Comparação com a mediana da indústria (pedido do Rui a 18/10/2026)
# ---------------------------------------------------------------------------
# Métricas que se podem comparar com a mediana: (coluna do quadro, grupo). O sentido («melhor» = acima ou abaixo da
# mediana) vem de filtros.METRICAS. Não há WACC: a Descoberta desconta todas as empresas à mesma taxa, por isso a
# mediana do WACC não diria nada (o ROIC já compara o retorno do capital).
METRICAS_MEDIANA = [
    ("ROIC mediano", "Rentabilidade"), ("ROE mediano", "Rentabilidade"), ("Margem bruta mediana", "Rentabilidade"),
    ("Margem FCF mediana", "Rentabilidade"),
    ("Crescimento receitas (CAGR)", "Crescimento"), ("Diluição anual", "Crescimento"),
    ("Dívida líquida/EBITDA", "Solidez"), ("Cobertura de juros", "Solidez"), ("Piotroski", "Solidez"),
    ("Altman Z", "Solidez"),
    ("Qualidade", "Notas da Descoberta"), ("Moat (nota)", "Notas da Descoberta"),
    ("Rendibilidade FCF", "Preço"), ("P/FCF", "Preço"), ("EV/EBIT", "Preço"), ("P/E", "Preço"), ("Desconto", "Preço"),
]
SO_POSITIVOS = {"P/E", "EV/EBIT", "P/FCF"}  # múltiplos negativos (prejuízo) não entram na mediana nem «cumprem»
MIN_EMPRESAS_OMISSAO = 15  # abaixo disto, a mediana da indústria é pouco fiável: recua-se para o setor
MIN_VALORES_METRICA = 5    # uma métrica com menos valores do que isto não tem mediana (não conta como critério)
GRUPOS_MEDIANA = {"regiao": "a mesma região da pesquisa", "eua_europa": "EUA, Canadá e Europa juntos"}


def metricas_mediana(crit: dict) -> list[str]:
    """As métricas escolhidas para comparar com a mediana (vazio se a comparação estiver desligada)."""
    m = crit.get("mediana") or {}
    if not m.get("ativo"):
        return []
    validas = {c for c, _ in METRICAS_MEDIANA}
    return [c for c in m.get("metricas") or [] if c in validas]


def sentido(coluna: str) -> str:
    """«acima» quando maior é melhor (ROIC), «abaixo» quando menor é melhor (P/E, dívida)."""
    return "acima" if METRICAS.get(coluna, {}).get("maior", True) else "abaixo"


def texto_mediana(crit: dict) -> str:
    m = crit.get("mediana") or {}
    alvo = (m.get("industria") or "").replace("—", " - ") or SETORES.get(m.get("setor"), m.get("setor") or "setor por escolher")
    return (f"melhor do que a mediana de {alvo} ({GRUPOS_MEDIANA.get(m.get('grupo'), GRUPOS_MEDIANA['regiao'])}) em: "
            + ", ".join(metricas_mediana(crit)))


def mesma_industria(a, b) -> bool:
    """O screener escreve «Software—Infrastructure»; a análise, «Software - Infrastructure»."""
    norm = lambda s: re.sub(r"\s+", " ", str(s or "").replace("—", " - ")).strip().lower()  # noqa: E731
    return bool(a) and norm(a) == norm(b)


def _valor_ok(coluna: str, v) -> bool:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return False
    return not (coluna in SO_POSITIVOS and v <= 0)


def calcular_medianas(df: pd.DataFrame, metricas: list[str]) -> list[dict]:
    """Mediana de cada métrica nas empresas do grupo de comparação, com o número de valores usados."""
    out = []
    for c in metricas:
        vals = [float(v) for v in (df[c] if c in df else []) if _valor_ok(c, v)]
        usada = len(vals) >= MIN_VALORES_METRICA
        out.append({"coluna": c, "sentido": sentido(c), "n": len(vals), "usada": usada,
                    "mediana": float(pd.Series(vals).median()) if usada else None,
                    "fmt": METRICAS.get(c, {}).get("fmt", "num")})
    return out


def _fmt_valor(m: dict, v: float) -> str:
    if m["fmt"] in ("pct", "pct0", "pp"):
        return f"{v * 100:.1f}%".replace(".", ",")
    if m["fmt"] == "x":
        return f"{v:.1f}x".replace(".", ",")
    return f"{v:.1f}".replace(".", ",")


def criterios_mediana(medianas: list[dict]) -> list[tuple]:
    """[(texto, função(linha) → cumpre?)]: melhor do que a mediana (estritamente). Sem o valor, não cumpre."""
    out = []
    for m in medianas:
        if not m["usada"]:
            continue

        def cumpre(linha, m=m):
            v = linha.get(m["coluna"])
            if not _valor_ok(m["coluna"], v):
                return False
            return v > m["mediana"] if m["sentido"] == "acima" else v < m["mediana"]

        out.append((f"{m['coluna']} {'>' if m['sentido'] == 'acima' else '<'} mediana ({_fmt_valor(m, m['mediana'])})", cumpre))
    return out


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
    total = len(condicoes_yahoo(crit)) + len(_criterios_depois(crit)) + len(metricas_mediana(crit))
    limiar = float(crit.get("limiar", 1.0))
    return total, (math.ceil(limiar * total - 1e-9) if total else 0)


def descricao(crit: dict) -> str:
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
    if metricas_mediana(crit):
        partes.append(texto_mediana(crit))
    total, minimo = contagem(crit)
    if total and minimo < total:
        partes.append(f"cumprir pelo menos {minimo} de {total} critérios ({float(crit.get('limiar', 1)):.0%})")
    return "; ".join(partes)


def executar(crit: dict, progresso=None) -> dict:
    """Corre a pesquisa completa. Devolve dict com df (empresas que passam), falhas, e contagens
    (universo, analisadas) para explicar o funil."""
    if metricas_mediana(crit):
        return executar_com_mediana(crit, progresso)
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


def _falhas_yahoo(regioes, mcap, setores, industrias, criterios_y, progresso=None) -> dict:
    """{texto do critério do Yahoo: nomes normalizados das empresas que o cumprem}, com uma consulta por critério
    (como em executar: o Yahoo pode devolver listagens diferentes da mesma empresa, por isso compara-se o nome)."""
    falhas: dict[str, list] = {}
    for i, (k, f, v) in enumerate(criterios_y, start=1):
        if progresso:
            progresso(i / max(len(criterios_y), 1), f"Critério {i} de {len(criterios_y)}: {_curto(f[2])}")
        cumprem = listar_universo(regioes, mcap, [(f[3], f[4], v)], setores, industrias=industrias)
        ok = set(cumprem["Empresa"].map(_nome_normalizado)) if not cumprem.empty else set()
        falhas[_texto_criterio(f, f[4], v)] = ok
    return falhas


def executar_com_mediana(crit: dict, progresso=None) -> dict:
    """
    Pesquisa pela mediana da indústria (pedido do Rui a 18/10/2026):
      1. Grupo de comparação: as empresas da indústria escolhida (ou do setor, se não houver indústria), com a
         capitalização mínima, na região da pesquisa ou, com o grupo «eua_europa», nos EUA, no Canadá e na Europa
         juntos. São analisadas (as maiores primeiro, até ao limite) e a mediana de cada métrica calcula-se nelas.
      2. Com menos de min_empresas analisadas na indústria, recua-se para o setor da mesma região (nunca para outra
         região) e diz-se porquê.
      3. Candidatas: as empresas da indústria (ou do setor) nas regiões da pesquisa. Cada métrica escolhida é um
         critério «melhor do que a mediana»; os outros critérios (Yahoo, depois da análise, moat) contam como sempre e
         o limiar é a percentagem que o Rui escolhe.
    Devolve o mesmo que executar, mais «mediana» (grupo, medianas e os valores de todas as candidatas, para a página
    da Pesquisa recalcular na hora quando se muda as métricas ou a percentagem).
    """
    from datetime import datetime

    med = crit.get("mediana") or {}
    regioes = [r for r in crit.get("regioes") or [] if r in REGIOES]
    if not regioes:
        raise ValueError("A pesquisa não tem regiões.")
    industria = med.get("industria") or None
    setor = med.get("setor") or ((crit.get("setores") or [None])[0] if len(crit.get("setores") or []) == 1 else None)
    if not industria and not setor:
        raise ValueError("Para comparar com a mediana, escolhe a indústria ou o setor.")
    grupo = med.get("grupo") if med.get("grupo") in GRUPOS_MEDIANA else "regiao"
    reg_pares = regioes if grupo == "regiao" else list(REGIOES)
    min_emp = int(med.get("min_empresas") or MIN_EMPRESAS_OMISSAO)
    mcap, limite = crit.get("mcap", 5000) * 1e6, int(crit.get("limite") or 200)
    p = parametros(crit)

    def analisar(regs, setores_, industrias_):
        uni = listar_universo(regs, mcap, [], setores_, progresso=progresso, industrias=industrias_)
        uni = ordenar(uni, ORDENS[0])  # as maiores primeiro: são as que definem a indústria
        tickers = uni["Ticker"].tolist()[:limite] if not uni.empty else []
        df_, falhas_ = analisar_lote(tickers, p, progresso=progresso, paises=paises_das_regioes(regs)) if tickers else (pd.DataFrame(), [])
        return df_, falhas_, len(uni)

    nivel, nota = ("industria", None) if industria else ("setor", None)
    pares, falhas, n_universo = analisar(reg_pares, [], [industria]) if industria else analisar(reg_pares, [setor], None)
    if industria and len(pares) < min_emp:
        if setor:
            nota = (f"só {len(pares)} empresas analisadas na indústria ({industria.replace('—', ' - ')}), abaixo do mínimo de "
                    f"{min_emp}: as medianas são as do setor {SETORES.get(setor, setor)}, na mesma região")
            pares_setor, falhas_s, n_universo = analisar(reg_pares, [setor], None)
            if len(pares_setor) > len(pares):
                pares, falhas, nivel = pares_setor, falhas + falhas_s, "setor"
        else:
            nota = f"só {len(pares)} empresas analisadas na indústria, abaixo do mínimo de {min_emp}: medianas pouco fiáveis"
    if len(pares) < min_emp and not nota:
        nota = f"só {len(pares)} empresas analisadas, abaixo do mínimo de {min_emp}: medianas pouco fiáveis"

    medianas = calcular_medianas(pares, metricas_mediana(crit))
    crit_med = criterios_mediana(medianas)
    paises_cand = paises_das_regioes(regioes)
    cand = pares
    if not cand.empty:
        cand = cand[cand["País"].isin(paises_cand)] if grupo != "regiao" else cand
        if industria and nivel == "setor":
            cand = cand[cand["Indústria"].map(lambda x: mesma_industria(industria, x))]

    # Os outros critérios: Yahoo (uma consulta por critério nas regiões da pesquisa) e os calculados depois da análise
    criterios_y = [(k, _YAHOO[k], v) for k, v in (crit.get("yahoo") or {}).items() if k in _YAHOO]
    ok_y = _falhas_yahoo(regioes, mcap, [] if industria else [setor], [industria] if industria else None, criterios_y, progresso)
    depois = _criterios_depois(crit)
    total = len(criterios_y) + len(depois) + len(crit_med)
    limiar = float(crit.get("limiar", 1.0))
    minimo = math.ceil(limiar * total - 1e-9) if total else 0
    colunas = [m["coluna"] for m in medianas]
    empresas, nao_cumpriu = [], []
    for _, linha in cand.iterrows():
        nome = _nome_normalizado(linha.get("Empresa"))
        outras = [t for t, ok in ok_y.items() if nome not in ok] + [t for t, cumpre in depois if not cumpre(linha)]
        da_mediana = [t for t, cumpre in crit_med if not cumpre(linha)]
        nao_cumpriu.append(outras + da_mediana)
        empresas.append({"Ticker": linha["Ticker"], "Empresa": linha.get("Empresa"), "País": linha.get("País"),
                         "Indústria": linha.get("Indústria"), "Classificação": linha.get("Classificação"),
                         "valores": {c: (float(linha[c]) if _valor_ok(c, linha.get(c)) else None) for c in colunas},
                         "outras_falhas": outras})
    df = cand.copy()
    if not df.empty and total:
        n_falhas = pd.Series([len(x) for x in nao_cumpriu], index=df.index)
        df = df.assign(**{"Critérios cumpridos": (total - n_falhas) / total,
                          "Critérios": [f"{total - n} de {total}" for n in n_falhas],
                          "Não cumpriu": ["; ".join(x) for x in nao_cumpriu]})
        df = df[n_falhas <= total - minimo].sort_values(["ordem", "Critérios cumpridos", "Preço (nota)"], ascending=[True, False, False])
    df = df.reset_index(drop=True) if not df.empty else df
    if not df.empty:
        df.attrs["descricao"] = descricao(crit)
    bloco = {"quando": datetime.now().isoformat(timespec="minutes"), "nivel": nivel, "industria": industria, "setor": setor,
             "grupo": grupo, "grupo_texto": GRUPOS_MEDIANA[grupo], "regioes_pares": reg_pares, "n_pares": len(pares),
             "min_empresas": min_emp, "mcap": crit.get("mcap", 5000), "nota": nota, "medianas": medianas,
             "outros_total": len(criterios_y) + len(depois), "limiar": limiar, "empresas": empresas}
    return {"df": df, "falhas": falhas, "universo": n_universo, "a_analisar": len(pares), "analisadas": len(cand),
            "filtros_depois": bool(depois), "criterios": total, "minimo": minimo, "descricao": descricao(crit), "mediana": bloco}
