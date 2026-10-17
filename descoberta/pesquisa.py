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
from filtros import FILTROS_ANALISE, FILTROS_YAHOO, FORCA_MOAT, SETORES, TIPOS_MOAT
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
    total = len(condicoes_yahoo(crit)) + len(_criterios_depois(crit))
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
    total, minimo = contagem(crit)
    if total and minimo < total:
        partes.append(f"cumprir pelo menos {minimo} de {total} critérios ({float(crit.get('limiar', 1)):.0%})")
    return "; ".join(partes)


def executar(crit: dict, progresso=None) -> dict:
    """Corre a pesquisa completa. Devolve dict com df (empresas que passam), falhas, e contagens
    (universo, analisadas) para explicar o funil."""
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
