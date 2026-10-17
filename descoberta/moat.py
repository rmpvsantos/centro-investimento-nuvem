"""
Moat (vantagem competitiva) a partir dos números.

Os números não mostram a origem de um moat (marca, patente, efeito de rede...), mas mostram
os seus efeitos: retornos persistentemente acima do custo de capital, margens que a
concorrência não consegue comprimir, receitas que se repetem, crescimento que exige pouco
capital. Este módulo mede esses efeitos em seis fontes, classifica o moat (largo, estreito,
sem moat evidente) e diz se está a reforçar-se ou a erodir.

Também sugere respostas para as perguntas qualitativas que os números conseguem informar;
as restantes (gestão, disrupção, dependências) continuam a depender da leitura do Rui.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from metricas import cagr


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def _lin(x: float, pontos: list[tuple[float, float]]) -> float:
    """Interpolação linear por troços: pontos = [(x, pontuação), ...] com x crescente."""
    xs, ys = zip(*pontos)
    return float(np.interp(x, xs, ys))


def _media(valores) -> float | None:
    v = [x for x in valores if _ok(x)]
    return float(np.mean(v)) if v else None


def _pp(v: float) -> str:
    return f"{v * 100:.1f}".replace(".", ",")


def _inicio_fim(s: pd.Series) -> tuple[float, float] | None:
    """Média dos 2 primeiros e dos 2 últimos exercícios (suaviza um ano atípico)."""
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < 3:
        return None
    return float(s.iloc[:2].mean()), float(s.iloc[-2:].mean())


def _nivel(p: float | None) -> str:
    if p is None:
        return "sem dados"
    return "Forte" if p >= 70 else "Moderado" if p >= 45 else "Fraco"


# ---------------------------------------------------------------------------
# Fontes do moat
# ---------------------------------------------------------------------------
def _fonte(nome, pontuacao, evidencias, explicacao, peso):
    return {"fonte": nome, "pontuacao": pontuacao, "nivel": _nivel(pontuacao), "evidencias": evidencias,
            "explicacao": explicacao, "peso": peso}


def _retorno_excedente(tab, r, financeira):
    serie = (tab["roe"] if financeira else tab["roic"]).replace([np.inf, -np.inf], np.nan).dropna()
    nome_r = "ROE" if financeira else "ROIC"
    expl = (f"A prova mais direta de um moat: um {nome_r} persistentemente acima do custo de capital. Sem vantagem "
            "competitiva, a concorrência faria descer os retornos até ao custo de capital.")
    if not financeira and len(tab) and (tab["capital_investido_medio"] <= 0).iloc[-1] and (tab["ebit"] > 0).iloc[-1]:
        return _fonte("Retorno acima do custo de capital", 100.0,
                      ["Capital investido negativo com lucro operacional: os clientes financiam o negócio."], expl, 35), 0.5, 1.0
    if len(serie) < 2:
        return _fonte("Retorno acima do custo de capital", None, ["Histórico insuficiente."], expl, 35), None, None
    spread = float(serie.median()) - r
    persist = float((serie > r).mean())
    p = _lin(spread, [(-0.05, 5), (0.0, 35), (0.05, 65), (0.10, 85), (0.15, 100)]) * (0.6 + 0.4 * persist)
    ev = [f"{nome_r} mediano de {_pp(serie.median())}% face a um custo de capital de {_pp(r)}%: "
          f"excedente de {_pp(spread)} p.p.",
          f"{nome_r} acima do custo de capital em {int((serie > r).sum())} de {len(serie)} exercícios."]
    return _fonte("Retorno acima do custo de capital", p, ev, expl, 35), spread, persist


def _poder_precos(tab):
    mb = tab["margem_bruta"].replace([np.inf, -np.inf], np.nan).dropna()
    expl = ("Uma margem bruta alta e estável mostra que a empresa cobra mais do que o custo de produzir, e que a "
            "concorrência não a consegue obrigar a baixar preços. É o sinal típico de marca ou produto diferenciado.")
    if len(mb) < 2:
        return _fonte("Poder de fixação de preços", None, ["Sem margem bruta reportada."], expl, 20)
    nivel = _lin(float(mb.median()), [(0.10, 10), (0.25, 40), (0.40, 70), (0.60, 100)])
    desvio = float(mb.std())
    estab = _lin(desvio, [(0.01, 100), (0.03, 70), (0.06, 35), (0.10, 10)])
    p = 0.6 * nivel + 0.4 * estab
    ev = [f"Margem bruta mediana de {_pp(mb.median())}%.",
          f"Variação entre anos (desvio-padrão) de {_pp(desvio)} p.p." + (" Muito estável." if desvio <= 0.02 else "")]
    return _fonte("Poder de fixação de preços", p, ev, expl, 20)


def _recorrencia(tab):
    expl = ("Receitas que se repetem (subscrições, contratos, pagamentos antecipados) e que não caem em anos maus "
            "indicam custos de mudança: para o cliente, trocar de fornecedor é caro ou arriscado.")
    partes, ev = [], []
    rd = tab["receita_diferida_receita"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(rd):
        v = float(rd.iloc[-1])
        partes.append(_lin(v, [(0.0, 20), (0.05, 50), (0.10, 75), (0.20, 100)]))
        ev.append(f"Receita diferida (paga antecipadamente pelos clientes) de {_pp(v)}% das receitas anuais.")
    cresc = tab["receita"].pct_change().dropna()
    if len(cresc):
        pior = float(cresc.min())
        partes.append(_lin(pior, [(-0.15, 10), (-0.05, 40), (0.0, 70), (0.05, 100)]))
        ev.append(f"Pior ano de receitas: {_pp(pior)}%" + (" (nunca caíram)." if pior >= 0 else "."))
    if not partes:
        return _fonte("Receitas recorrentes e custos de mudança", None, ["Sem dados."], expl, 10)
    return _fonte("Receitas recorrentes e custos de mudança", float(np.mean(partes)), ev, expl, 10)


def _escala(tab):
    expl = ("Quando as margens operacionais sobem à medida que as receitas crescem, os custos fixos estão a ser "
            "diluídos: vantagem de escala que concorrentes mais pequenos não conseguem igualar.")
    me = tab["margem_ebit"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(me) < 2:
        return _fonte("Escala e alavancagem operacional", None, ["Sem margem operacional."], expl, 15)
    nivel = _lin(float(me.median()), [(0.0, 5), (0.05, 25), (0.12, 50), (0.20, 75), (0.30, 100)])
    ev = [f"Margem operacional (EBIT) mediana de {_pp(me.median())}%."]
    fi = _inicio_fim(me)
    c = cagr(tab["receita"])
    if fi and _ok(c):
        delta = fi[1] - fi[0]
        if c > 0 and delta > 0.01:
            alav = 100
        elif delta >= -0.01:
            alav = 55
        else:
            alav = 15
        ev.append(f"Margem operacional {'+' if delta > 0 else ''}{_pp(delta)} p.p. entre o início e o fim do período, com "
                  f"receitas a crescer {_pp(c)}% ao ano.")
        p = 0.6 * nivel + 0.4 * alav
    else:
        p = nivel
    return _fonte("Escala e alavancagem operacional", p, ev, expl, 15)


def _eficiencia(tab):
    expl = ("Negócios que precisam de pouco capital para funcionar (baixo investimento em ativos fixos) e que "
            "convertem o lucro em caixa podem crescer e remunerar os acionistas ao mesmo tempo.")
    partes, ev = [], []
    cx = tab["capex_receita"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(cx):
        v = float(cx.median())
        partes.append(_lin(v, [(0.0, 100), (0.03, 100), (0.06, 75), (0.10, 45), (0.20, 10)]))
        ev.append(f"Investimento em ativos fixos (capex) de {_pp(v)}% das receitas.")
    conv = tab["conversao_fcf"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(conv):
        v = float(conv.median())
        partes.append(_lin(v, [(0.3, 10), (0.6, 45), (0.8, 75), (1.0, 100)]))
        ev.append(f"FCF igual a {v:.2f}x o lucro (mediana).".replace(".", ",", 1))
    if not partes:
        return _fonte("Eficiência de capital", None, ["Sem dados."], expl, 10)
    return _fonte("Eficiência de capital", float(np.mean(partes)), ev, expl, 10)


def _reinvestimento(tab, r):
    expl = ("O que alarga um moat é reinvestir a retornos altos: cada euro novo investido tem de render bem acima "
            "do custo de capital (ROIC incremental). Investigação e desenvolvimento sustentado reforça a vantagem.")
    nop = tab["nopat"].dropna()
    ic = tab["capital_investido_medio"].dropna()
    ev, p, roic_inc = [], None, None
    if len(nop) >= 3 and len(ic) >= 3:
        d_nop = float(nop.iloc[-1] - nop.iloc[0])
        d_ic = float(ic.iloc[-1] - ic.iloc[0])
        if d_ic <= 0 and d_nop > 0:
            p, roic_inc = 100.0, float("inf")
            ev.append("O lucro operacional cresceu sem aumento do capital investido: crescimento quase sem capital.")
        elif d_ic > 0:
            roic_inc = d_nop / d_ic
            p = _lin(roic_inc, [(r - 0.05, 5), (r, 30), (r + 0.05, 60), (0.25, 85), (0.40, 100)])
            ev.append(f"ROIC incremental de {_pp(roic_inc)}%: retorno do capital investido a mais no período "
                      f"(custo de capital {_pp(r)}%).")
        else:
            p = 20.0
            ev.append("Lucro operacional e capital investido a descer.")
    rd = tab["investigacao_receita"].replace([np.inf, -np.inf], np.nan).dropna()
    if len(rd):
        ev.append(f"Investigação e desenvolvimento de {_pp(rd.iloc[-1])}% das receitas.")
    if p is None and not ev:
        ev = ["Histórico insuficiente."]
    return _fonte("Reinvestimento a retornos altos", p, ev, expl, 10), roic_inc


# ---------------------------------------------------------------------------
# Tendência: o moat está a reforçar-se?
# ---------------------------------------------------------------------------
def _tendencia(tab, financeira):
    sinais = []

    def comparar(nome, serie, limiar, bom_se_sobe=True):
        fi = _inicio_fim(serie)
        if not fi:
            return
        d = fi[1] - fi[0]
        direcao = 0 if abs(d) < limiar else (1 if (d > 0) == bom_se_sobe else -1)
        seta = "sobe" if d > 0 else "desce"
        sinais.append({"sinal": nome, "direcao": direcao,
                       "detalhe": f"{seta} de {_pp(fi[0])}% para {_pp(fi[1])}% (média dos 2 primeiros vs 2 últimos anos)"})

    comparar("ROE" if financeira else "ROIC", tab["roe"] if financeira else tab["roic"], 0.02)
    if not financeira:
        comparar("Margem bruta", tab["margem_bruta"], 0.01)
        comparar("Margem operacional", tab["margem_ebit"], 0.015)
        comparar("Investimento em I&D / receitas", tab["investigacao_receita"], 0.005)
        comparar("Receita diferida / receitas", tab["receita_diferida_receita"], 0.01)
    c = cagr(tab["receita"])
    if _ok(c):
        sinais.append({"sinal": "Crescimento das receitas", "direcao": 1 if c >= 0.05 else -1 if c < 0 else 0,
                       "detalhe": f"{_pp(c)}% ao ano"})
    if not sinais:
        return "nd", "Sem dados", sinais
    total = sum(s["direcao"] for s in sinais)
    if total >= 2:
        return "reforco", "A reforçar-se", sinais
    if total <= -2:
        return "erosao", "A erodir", sinais
    return "estavel", "Estável", sinais


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def analisar_moat(res: dict, taxa_desconto: float = 0.09) -> dict:
    tab = res["anual"]
    financeira = bool(res.get("modelo_financeiro"))
    r = taxa_desconto

    ret, spread, persist = _retorno_excedente(tab, r, financeira)
    fontes = [ret]
    roic_inc = None
    if not financeira:
        rein, roic_inc = _reinvestimento(tab, r)
        fontes += [_poder_precos(tab), _escala(tab), _recorrencia(tab), _eficiencia(tab), rein]

    com_dados = [f for f in fontes if f["pontuacao"] is not None]
    peso = sum(f["peso"] for f in com_dados)
    pont = sum(f["pontuacao"] * f["peso"] for f in com_dados) / peso if peso else None

    if pont is None or spread is None:
        classe, titulo = "nd", "Sem dados suficientes"
    elif spread >= 0.10 and persist == 1.0 and pont >= 70:
        classe, titulo = "largo", "Moat largo"
    elif spread >= 0.03 and persist >= 0.75 and pont >= 50:
        classe, titulo = "estreito", "Moat estreito"
    else:
        classe, titulo = "sem", "Sem moat evidente"

    tend, tend_titulo, sinais = _tendencia(tab, financeira)
    return {
        "classe": classe, "titulo": titulo, "pontuacao": pont, "fontes": fontes,
        "tendencia": tend, "tendencia_titulo": tend_titulo, "sinais": sinais,
        "sugestoes": _sugestoes(res, classe, tend, fontes, roic_inc, r),
        "custo_capital": r,
    }


# ---------------------------------------------------------------------------
# Sugestões para a avaliação qualitativa
# ---------------------------------------------------------------------------
def _sugestoes(res, classe, tend, fontes, roic_inc, r) -> dict:
    """{chave da pergunta: (0 | 1 | 2, justificação)} — só para as perguntas que os números informam."""
    tab = res["anual"]
    s = {}
    if classe != "nd":
        s["vantagem"] = ({"largo": 2, "estreito": 1, "sem": 0}[classe],
                         ("Os números não mostram um moat evidente." if classe == "sem" else
                          f"Os números indicam um moat {classe}.")
                         + " Confirma se consegues nomear a fonte (marca, rede, custos de mudança, escala, patentes).")
    if tend != "nd":
        s["vantagem_trend"] = ({"reforco": 2, "estavel": 1, "erosao": 0}[tend],
                               {"reforco": "Retornos, margens ou investimento a subir.",
                                "estavel": "Sem mudanças claras nos retornos e margens.",
                                "erosao": "Retornos ou margens a descer: a concorrência pode estar a ganhar terreno."}[tend])

    c = cagr(tab["receita"])
    an = [v["crescimento"] for v in (res.get("crescimento_analistas") or {}).values() if _ok(v.get("crescimento"))]
    g = _media([c] + ([float(np.mean(an))] if an else []))
    if g is not None:
        s["mercado"] = (2 if g >= 0.08 else 1 if g >= 0.03 else 0,
                        f"Crescimento das receitas (histórico e previsões) de {_pp(g)}% ao ano. "
                        "Confirma se vem do mercado ou de ganhos de quota.")

    cresc = tab["receita"].pct_change().dropna()
    rec = next((f["pontuacao"] for f in fontes if f["fonte"].startswith("Receitas recorrentes")), None)
    if len(cresc):
        pior = float(cresc.min())
        v = 2 if pior >= 0 and (rec or 0) >= 50 else 1 if pior >= -0.05 else 0
        s["recessao"] = (v, f"Pior ano de receitas no período: {_pp(pior)}%. O Yahoo só dá 4 a 5 anos: "
                            "verifica também 2008-09 e 2020 nos relatórios.")

    acoes = tab["acoes"].dropna()
    dil = None
    if len(acoes) >= 2 and acoes.iloc[0] > 0:
        anos = max((acoes.index[-1] - acoes.index[0]).days / 365.25, 1)
        dil = (acoes.iloc[-1] / acoes.iloc[0]) ** (1 / anos) - 1
    if roic_inc is not None or dil is not None:
        bom_inc = roic_inc is not None and roic_inc >= r + 0.05
        bom_dil = dil is not None and dil <= 0.005
        partes = []
        if roic_inc is not None:
            partes.append("ROIC incremental " + ("sem necessidade de capital" if math.isinf(roic_inc)
                                                  else f"de {_pp(roic_inc)}%"))
        if dil is not None:
            partes.append(f"número de ações {_pp(dil)}% ao ano")
        texto = "; ".join(partes)
        s["capital"] = (2 if bom_inc and bom_dil else 1 if bom_inc or bom_dil else 0, texto[0].upper() + texto[1:] + ".")

    ins = res.get("insiders")
    if _ok(ins):
        s["alinhamento"] = (2 if ins >= 0.05 else 1 if ins >= 0.01 else 0,
                            f"Gestores e administradores detêm {_pp(ins)}% das ações. Em empresas muito grandes, "
                            "valores baixos são normais: vê também a política de remuneração.")
    return s
