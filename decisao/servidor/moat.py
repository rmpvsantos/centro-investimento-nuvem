"""
Análise do moat (vantagem competitiva duradoura), sem rede: recebe as demonstrações financeiras e as medianas dos
concorrentes já obtidas.

  FORÇA (0 a 10): seis testes quantitativos sobre 4 exercícios —
     1. retorno acima do custo do capital (ROIC médio − WACC)            até 3 pontos
     2. persistência (em quantos anos o ROIC superou o WACC)              até 2
     3. poder de preço (estabilidade da margem bruta)                     até 2
     4. tendência do ROIC (não se degrada)                                até 1
     5. margem bruta acima da mediana dos concorrentes                    até 1
     6. ROIC acima da mediana dos concorrentes                            até 1
     A nota quantitativa é a média com a nota do fosso dos teus relatórios, quando existe.
     Largo: 7,5 ou mais · Estreito: 5 a 7,5 · Sem moat evidente: abaixo de 5.

  TENDÊNCIA: a reforçar / estável / a erodir, pela evolução do ROIC, da margem bruta e da tua nota do fosso.

  TIPO: custos de mudança, efeitos de rede, ativos intangíveis, vantagem de custos, escala eficiente — pontuados pelas
  menções nos teus relatórios, pela indústria e por indícios quantitativos próprios de cada tipo. Tu confirmas.

  RISCOS: sinais de erosão (ROIC ou margens a cair, diluição, remuneração em ações pesada).

Cortes arbitrários: é uma grelha de trabalho, não uma avaliação definitiva.
"""
from __future__ import annotations

import math
import statistics

import pandas as pd

from metricas import _sem_fuso, alinhar, linha
from qualitativa import INDUSTRIA_MOAT, TIPOS_MOAT

NAN = float("nan")
CLASSES = {"largo": "Largo", "estreito": "Estreito", "nenhum": "Sem moat evidente"}


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and not math.isinf(v)


def _pp(v: float) -> str:
    return f"{'+' if v > 0 else ''}{v * 100:.1f} p.p.".replace(".", ",", 1)


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%".replace(".", ",")


def _linha_nomes(df, nomes: list[str]) -> pd.Series:
    """Como metricas.linha, para rótulos que o motor não conhece (ex.: I&D)."""
    if df is None or getattr(df, "empty", True):
        return pd.Series(dtype=float)
    for n in nomes:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = _sem_fuso(s.index)
            return s.dropna().sort_index()
    return pd.Series(dtype=float)


def _lista(s) -> list:
    return [float(v) if _ok(float(v)) else None for v in s]


def series_anuais(dados: dict, anual: pd.DataFrame) -> dict:
    """Evolução anual das métricas que revelam um moat: ROIC, margens, intensidade de I&D e de capex."""
    datas = anual.index
    inc, cf = dados.get("income"), dados.get("cashflow")
    receita = anual["receita"]
    bruto = alinhar(linha(inc, "lucro_bruto"), datas)
    ebit = alinhar(linha(inc, "ebit"), datas)
    rd = alinhar(_linha_nomes(inc, ["Research And Development", "Research Development"]), datas)
    capex = alinhar(linha(cf, "capex"), datas).abs()
    div = lambda a, b: (a / b).where(b > 0)  # noqa: E731
    return {
        "anos": [f"{d:%Y}" for d in datas],
        "roic": _lista(anual["roic"]),
        "crescimento": _lista(anual["crescimento_receita"]),
        "margem_bruta": _lista(div(bruto, receita)),
        "margem_operacional": _lista(div(ebit, receita)),
        "i_d": _lista(div(rd, receita)),
        "capex": _lista(div(capex, receita)),
    }


def _validos(lista) -> list[float]:
    return [v for v in lista or [] if _ok(v)]


def _delta(lista) -> float:
    v = _validos(lista)
    return v[-1] - v[0] if len(v) >= 2 else NAN


def forca(series: dict, wacc: float, valores: dict, medianas: dict) -> dict:
    comp = []

    def c(nome, pontos, maximo, leitura):
        comp.append({"criterio": nome, "pontos": pontos, "maximo": maximo, "leitura": leitura})

    roic = _validos(series.get("roic"))
    if roic and _ok(wacc):
        sp = statistics.mean(roic) - wacc
        c("Retorno acima do custo do capital", 3 if sp >= 0.10 else 2 if sp >= 0.05 else 1 if sp > 0 else 0, 3,
          f"ROIC médio {_pct(statistics.mean(roic))} face a um WACC de {_pct(wacc)} (diferença {_pp(sp)})")
        acima = sum(1 for r in roic if r > wacc)
        c("Persistência do retorno", 2 if acima == len(roic) else 1 if acima >= len(roic) / 2 else 0, 2,
          f"ROIC acima do WACC em {acima} de {len(roic)} exercícios")
    else:
        c("Retorno acima do custo do capital", None, 3, "sem ROIC ou WACC")
        c("Persistência do retorno", None, 2, "sem ROIC ou WACC")

    mb = _validos(series.get("margem_bruta"))
    if len(mb) >= 3:
        dp = statistics.pstdev(mb)
        c("Poder de preço (estabilidade da margem bruta)", 2 if dp <= 0.02 else 1 if dp <= 0.05 else 0, 2,
          f"margem bruta entre {_pct(min(mb))} e {_pct(max(mb))} (desvio-padrão {dp * 100:.1f} p.p.)".replace(".", ",", 1))
    else:
        c("Poder de preço (estabilidade da margem bruta)", None, 2, "sem margem bruta (comum em bancos e seguradoras)")

    d = _delta(series.get("roic"))
    c("Tendência do ROIC", None if not _ok(d) else 1 if d >= -0.02 else 0, 1,
      "sem histórico" if not _ok(d) else f"ROIC {'estável ou a subir' if d >= -0.02 else 'a cair'} ({_pp(d)} no período)")

    for chave, nome in (("margem_bruta", "Margem bruta face aos concorrentes"), ("roic", "ROIC face aos concorrentes")):
        v, m = valores.get(chave), medianas.get(chave)
        if _ok(v) and _ok(m):
            c(nome, 1 if v > m else 0, 1, f"{_pct(v)} face à mediana de {_pct(m)}")
        else:
            c(nome, None, 1, "sem mediana dos concorrentes")

    avaliados = [x for x in comp if x["pontos"] is not None]
    maximo = sum(x["maximo"] for x in avaliados)
    nota = round(sum(x["pontos"] for x in avaliados) / maximo * 10, 1) if maximo >= 4 else None
    return {"componentes": comp, "nota": nota, "pontos": sum(x["pontos"] for x in avaliados), "maximo": maximo}


def tendencia(series: dict, historico_fosso: list[float]) -> dict:
    sinais = []
    d = _delta(series.get("roic"))
    if _ok(d) and abs(d) >= 0.03:
        sinais.append((2 if abs(d) >= 0.05 else 1) * (1 if d > 0 else -1))
    d2 = _delta(series.get("margem_bruta"))
    if _ok(d2) and abs(d2) >= 0.02:
        sinais.append(1 if d2 > 0 else -1)
    h = [v for v in historico_fosso if _ok(v)]
    if len(h) >= 2 and h[-1] != h[-2]:
        sinais.append(1 if h[-1] > h[-2] else -1)
    s = sum(sinais)
    estado = "a reforçar" if s >= 2 else "a erodir" if s <= -2 else "estável"
    partes = []
    if _ok(d):
        partes.append(f"ROIC {_pp(d)}")
    if _ok(d2):
        partes.append(f"margem bruta {_pp(d2)}")
    if len(h) >= 2:
        partes.append(f"a tua nota do fosso passou de {h[-2]:g} para {h[-1]:g}".replace(".", ","))
    return {"estado": estado, "sinal": s, "leitura": "; ".join(partes) or "sem histórico suficiente"}


def indicios_quantitativos(series: dict, valores: dict, medianas: dict) -> dict[str, list[str]]:
    """Indícios, nos números, de cada tipo de moat (reforçam a sugestão; não chegam para o provar)."""
    ind = {k: [] for k in TIPOS_MOAT}
    mb, mo = _validos(series.get("margem_bruta")), _validos(series.get("margem_operacional"))
    cres, idd, capex = _validos(series.get("crescimento")), _validos(series.get("i_d")), _validos(series.get("capex"))
    roic = _validos(series.get("roic"))
    wacc = valores.get("wacc")
    if mb and mb[-1] >= 0.60:
        ind["intangiveis"].append(f"margem bruta de {_pct(mb[-1])}: o cliente paga bem acima do custo")
    if idd and idd[-1] >= 0.10:
        ind["intangiveis"].append(f"I&D de {_pct(idd[-1])} da receita: tecnologia e propriedade intelectual")
    if mb and mb[-1] >= 0.50 and cres and all(c > 0 for c in cres):
        ind["custos_mudanca"].append("margem bruta alta e receita a crescer todos os anos: os clientes ficam")
    if len(mo) >= 2 and mo[-1] - mo[0] >= 0.03 and cres and statistics.mean(cres) >= 0.10:
        ind["efeitos_rede"].append(f"margem operacional a expandir ({_pp(mo[-1] - mo[0])}) com receita a crescer {_pct(statistics.mean(cres))} ao ano")
    me, mm_b = medianas.get("margem_ebitda"), medianas.get("margem_bruta")
    if _ok(valores.get("margem_ebitda")) and _ok(me) and _ok(valores.get("margem_bruta")) and _ok(mm_b) \
            and valores["margem_ebitda"] > me and valores["margem_bruta"] <= mm_b:
        ind["custos"].append("margem EBITDA acima dos concorrentes com margem bruta igual ou inferior: é mais eficiente")
    if _ok(valores.get("rotacao_ativo")) and _ok(medianas.get("rotacao_ativo")) and valores["rotacao_ativo"] > medianas["rotacao_ativo"] * 1.2:
        ind["custos"].append("rotação do ativo mais de 20% acima dos concorrentes: faz mais com menos capital")
    if capex and statistics.mean(capex) >= 0.08 and roic and _ok(wacc) and statistics.mean(roic) > wacc and cres and statistics.mean(cres) <= 0.06:
        ind["escala_eficiente"].append("negócio intensivo em capital, crescimento baixo e retorno acima do custo do capital: típico de mercados com poucos operadores")
    return ind


def riscos(series: dict, valores: dict) -> list[str]:
    r = []
    d = _delta(series.get("roic"))
    if _ok(d) and d <= -0.03:
        r.append(f"ROIC a cair ({_pp(d)} no período): a vantagem pode estar a ser competida")
    d2 = _delta(series.get("margem_bruta"))
    if _ok(d2) and d2 <= -0.02:
        r.append(f"margem bruta a cair ({_pp(d2)}): menos poder de preço ou custos a subir")
    cres = _validos(series.get("crescimento"))
    if cres and cres[-1] < 0:
        r.append(f"receita a cair no último exercício ({_pct(cres[-1])})")
    if _ok(valores.get("sbc_fcf")) and valores["sbc_fcf"] >= 0.20:
        r.append(f"remuneração em ações = {_pct(valores['sbc_fcf'])} do FCF: parte do retorno vai para os gestores")
    if _ok(valores.get("var_acoes")) and valores["var_acoes"] >= 0.03:
        r.append(f"número de ações a subir ({_pct(valores['var_acoes'])} no período): diluição dos acionistas")
    if _ok(valores.get("spread")) and valores["spread"] <= 0:
        r.append("ROIC abaixo do custo do capital: o crescimento destrói valor")
    return r


def avaliar(info: dict, series: dict | None, valores: dict | None, medianas: dict, evid: dict, fosso_wiki: dict | None,
            historico_fosso: list[float], manual: dict | None = None) -> dict:
    manual, valores, series = manual or {}, valores or {}, series or {}
    industria = (info.get("industry") or "").lower()

    f = forca(series, valores.get("wacc", NAN), valores, medianas) if series else {"componentes": [], "nota": None, "pontos": 0, "maximo": 0}
    fontes = []
    if _ok(f["nota"]):
        fontes.append({"fonte": "Testes quantitativos (4 exercícios)", "nota": f["nota"]})
    if fosso_wiki and _ok(fosso_wiki.get("valor")):
        fontes.append({"fonte": "Nota do fosso nos teus relatórios", "nota": float(fosso_wiki["valor"])})
    nota = round(sum(x["nota"] for x in fontes) / len(fontes), 1) if fontes else None
    classe_auto = None if nota is None else "largo" if nota >= 7.5 else "estreito" if nota >= 5 else "nenhum"
    classe = manual.get("classe") if manual.get("classe") in CLASSES else classe_auto

    tend = tendencia(series, historico_fosso) if series else {"estado": None, "sinal": 0, "leitura": "sem dados"}
    tend_final = manual.get("tendencia") if manual.get("tendencia") in ("a reforçar", "estável", "a erodir") else tend["estado"]

    tipicos = {t for chaves, tipos in INDUSTRIA_MOAT if any(k in industria for k in chaves) for t in tipos}
    indicios = indicios_quantitativos(series, valores, medianas) if series else {k: [] for k in TIPOS_MOAT}
    tipos = []
    for chave, (nome, descricao, _termos) in TIPOS_MOAT.items():
        mencoes = evid.get(chave, {}).get("mencoes", 0)
        razoes, pts = [], 0
        if mencoes:
            pts += min(3, 1 + mencoes // 3)
            razoes.append(f"{mencoes} menções nos teus relatórios")
        if chave in tipicos:
            pts += 2
            razoes.append(f"típico da indústria ({info.get('industry')})")
        for i in indicios[chave]:
            pts += 1
            razoes.append(i)
        tipos.append({"chave": chave, "nome": nome, "descricao": descricao, "pontos": pts, "razoes": razoes,
                      "sugerido": pts >= 2, "excertos": evid.get(chave, {}).get("excertos", [])})
    tipos.sort(key=lambda t: -t["pontos"])

    return {
        "nota": nota, "fontes": fontes, "classe_auto": classe_auto, "classe": classe,
        "classe_substituida": classe is not None and classe != classe_auto,
        "forca": f, "tendencia": tend_final, "tendencia_auto": tend["estado"], "tendencia_leitura": tend["leitura"],
        "tipos": tipos, "tipos_confirmados": [t for t in manual.get("tipos", []) if t in TIPOS_MOAT],
        "riscos": riscos(series, valores) if series else [], "evolucao": series, "wacc": valores.get("wacc"),
        "fosso_wiki": fosso_wiki, "historico_fosso": historico_fosso, "nota_manual": manual.get("nota") or "",
    }
