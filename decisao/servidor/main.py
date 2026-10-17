"""
Servidor da app de decisão de investimento. Correr: .venv\\Scripts\\python.exe -m uvicorn servidor.main:app --port 8600

Junta o motor da Triagem (copiado para servidor/motor), a análise técnica, a análise qualitativa, a decisão por regras e a
wiki dos relatórios, e serve a interface (web/dist) no mesmo endereço.
"""
from __future__ import annotations

import math
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(RAIZ / "servidor" / "motor"), str(RAIZ / "servidor")]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import calendario as CAL  # noqa: E402
import compras as CP  # noqa: E402
import por_decidir as PD  # noqa: E402
import config  # noqa: E402
import criterios_config as C  # noqa: E402
import dados as DAD  # noqa: E402
import decisao as DEC  # noqa: E402
import fundamental as F  # noqa: E402
import listas as L  # noqa: E402
import metricas as M  # noqa: E402
import moat as MO  # noqa: E402
import noticias as N  # noqa: E402
import pares as P  # noqa: E402
import qualitativa as Q  # noqa: E402
import referencia as R  # noqa: E402
import regras as RG  # noqa: E402
import regras_cowork as RC  # noqa: E402
import tecnica as T  # noqa: E402
import traducao as TR  # noqa: E402
import wiki as W  # noqa: E402

C.FICHEIRO = config.pasta_dados() / "criterios.json"  # os limiares da triagem ficam com os restantes dados

app = FastAPI(title="Decisão de Investimento")
PREMIO_RISCO = 0.05
TAXA_OMISSAO = 0.04


# ---------------------------------------------------------------------------
# Cache em memória com validade, um cadeado por chave (dois pedidos iguais em paralelo só vão uma vez ao Yahoo)
# ---------------------------------------------------------------------------
_cache: dict = {}
_cadeados: dict = {}
_cadeado_global = threading.Lock()


def em_cache(horas: float):
    def deco(fn):
        def envolvido(*args, renovar: bool = False):
            chave = (fn.__name__, args)
            with _cadeado_global:
                cad = _cadeados.setdefault(chave, threading.Lock())
            with cad:
                hit = _cache.get(chave)
                if hit and not renovar and time.time() - hit[0] < horas * 3600:
                    return hit[1]
                valor = fn(*args)
                _cache[chave] = (time.time(), valor)
                return valor
        envolvido.__name__ = fn.__name__
        return envolvido
    return deco


def limpo(o):
    """Converte o resultado do motor (pandas, numpy, NaN, datas) em JSON."""
    if isinstance(o, dict):
        return {str(k): limpo(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [limpo(v) for v in o]
    if isinstance(o, pd.DataFrame):
        return limpo(o.reset_index().to_dict(orient="records"))
    if isinstance(o, pd.Series):
        return limpo(o.to_dict())
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()[:10]
    if o is pd.NaT:
        return None
    return o


def _t(ticker: str) -> str:
    t = ticker.strip().upper()
    if not t or len(t) > 20:
        raise HTTPException(400, "Ticker inválido")
    return t


def _falha(e: Exception):
    raise HTTPException(502, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Dados (Yahoo), com cache
# ---------------------------------------------------------------------------
def limiares() -> dict:
    return C.carregar()


@em_cache(6)
def dados(t: str) -> dict:
    return DAD.obter_dados(t)


@em_cache(6)
def analise(t: str, lim: tuple) -> dict:
    return M.analisar(dados(t), dict(lim))


@em_cache(1)
def taxa_sem_risco() -> float:
    return DAD.obter_taxa_sem_risco() or TAXA_OMISSAO


@em_cache(1)
def historico(t: str) -> dict:
    return DAD.obter_historico(t)


@em_cache(1)
def consenso(t: str) -> dict:
    sa = DAD.obter_html_stockanalysis(t)
    interp = R.interpretar_stockanalysis(sa["html"])
    return {"yahoo": DAD.obter_consenso_yahoo(t), "stockanalysis": interp,
            "erro_stockanalysis": sa["erro"] or (None if interp else "a página do StockAnalysis abriu, mas não encontrei o preço-alvo")}


@em_cache(0.25)
def preco_minuto(t: str) -> dict:
    """Último preço negociado no Yahoo (velas de 1 minuto do último dia de bolsa), com o momento. Pence passam a libras."""
    import yfinance as yf

    tk = yf.Ticker(t)
    h = tk.history(period="1d", interval="1m")
    s = h["Close"].dropna() if h is not None and not h.empty else None
    if s is None or s.empty:
        raise ValueError("o Yahoo não devolveu preços de 1 minuto")
    try:
        moeda = tk.fast_info["currency"]
    except Exception:  # noqa: BLE001
        moeda = None
    preco = float(s.iloc[-1])
    if moeda == "GBp":
        preco, moeda = preco / 100, "GBP"
    return {"preco": preco, "moeda": moeda, "momento": pd.Timestamp(s.index[-1])}


@em_cache(6)
def transacoes_insiders(t: str) -> list[dict]:
    import yfinance as yf

    try:
        df = yf.Ticker(t).insider_transactions
    except Exception:  # noqa: BLE001
        return []
    if df is None or df.empty:
        return []
    return [{"data": str(r.get("Start Date"))[:10], "insider": r.get("Insider"), "cargo": r.get("Position"),
             "texto": r.get("Text"), "acoes": r.get("Shares"), "valor": r.get("Value")} for _, r in df.iterrows()]


@em_cache(6)
def calendario_bruto(t: str) -> tuple:
    import yfinance as yf

    tk = yf.Ticker(t)
    try:
        cal = tk.calendar or {}
    except Exception:  # noqa: BLE001
        cal = {}
    try:
        datas = tk.get_earnings_dates(limit=12)
    except Exception:  # noqa: BLE001
        datas = None
    return cal, datas


def bloco_calendario(t: str) -> dict:
    cal, datas = calendario_bruto(t)
    c = CAL.montar(cal, datas)
    indice = W.construir_indice()
    rels = [r for r in W.relatorios_da_empresa(indice, W.empresa_por_ticker(indice, t))
            if not r["arquivado"] and r["tipo"] in ("trimestral", "acompanhamento", "iniciacao", "fundamental")]
    data_rel = max((r["data"] for r in rels), default=None)
    c["relatorio"] = {**CAL.estado_relatorio((c["ultimo"] or {}).get("data"), data_rel, bool(rels)), "data_ultimo": data_rel}
    return {"ticker": t, **c}


def pares_manuais(t: str) -> list[str] | None:
    return W.ler_notas(t).get("pares")


@em_cache(6)
def grupo(t: str, lim: tuple, manuais: tuple | None) -> dict:
    """Concorrentes como na Triagem: os escolhidos à mão ou as maiores da indústria (completadas com as do setor)."""
    fn = lambda s: analise(s, lim)  # noqa: E731
    if manuais:
        g = P.montar_grupo_manual(t, list(manuais), fn)
        g["base"] = "manual"
    else:
        res = fn(t)
        cand = DAD.obter_candidatos(res.get("industria_key"), res.get("setor_key"))
        ind = [s for s in cand["industria"] if s != t]
        g = P.montar_grupo(t, ind, fn) if ind else {"pares": [], "analises": [], "falhas": [], "estatisticas": {}}
        g["base"] = "industria"
        if len(g["pares"]) < 3:
            g = P.montar_grupo(t, ind + [s for s in cand["setor"] if s not in ind], fn)
            g["base"] = "industria_setor" if ind else "setor"
    g["analises"] = [{"ticker": a["ticker"], "nome": a["nome"], "chaves": a["chaves"], "score": a["score"],
                      "score_max": a["score_max"]} for a in g["analises"]]
    return g


def _lim() -> tuple:
    return tuple(sorted(limiares().items()))


def _grupo(t: str) -> dict:
    m = pares_manuais(t)
    return grupo(t, _lim(), tuple(m) if m else None)


@em_cache(6)
def fundamental_empresa(t: str, taxa: float, lim: tuple) -> dict:
    return F.calcular(dados(t), analise(t, lim), taxa, PREMIO_RISCO)


# ---------------------------------------------------------------------------
# Blocos de análise
# ---------------------------------------------------------------------------
def bloco_triagem(t: str) -> dict:
    lim = limiares()
    res = analise(t, _lim())
    criterios, score, score_max = res["criterios"], res["score"], res["score_max"]
    g = {"pares": [], "analises": [], "falhas": [], "base": None}
    aviso_grupo = None
    try:
        g = _grupo(t)
        if g["analises"]:
            criterios, score, score_max = P.aplicar_grupo(criterios, g, lim["pe_cumpre"], lim["pe_parcial"])
    except Exception as e:  # noqa: BLE001
        aviso_grupo = f"Não foi possível construir o grupo de concorrentes: {e}"

    # As tuas regras: tira os critérios desligados, junta os teus e volta a pontuar
    regras = RG.ler()
    valores = None
    if regras["personalizados"]:
        try:
            valores = fundamental_empresa(t, taxa_sem_risco(), _lim())["valores"]
        except Exception:  # noqa: BLE001
            valores = None
    criterios = RG.aplicar(criterios, valores, regras)
    score, score_max = M.pontuar(criterios)
    veredicto = M.veredicto(criterios, score, score_max)
    elim = [c["criterio"] for c in criterios if c.get("personalizado") and c.get("eliminatorio") and c["estado"] == "falha"]
    if elim and veredicto["nivel"] == "sim":
        veredicto = {**veredicto, "nivel": "reservas", "titulo": "Análise aprofundada com reservas",
                     "pontos": veredicto["pontos"] + [f"Falha em critério eliminatório teu: {', '.join(elim)}."]}

    anual = res["anual"].copy()
    anual.index = [f"{d:%Y}" for d in anual.index]
    return {
        "ticker": t, "nome": res["nome"], "setor": res["setor"], "industria": res["industria"], "moeda": res["moeda"],
        "preco": res["preco_atual"], "capitalizacao": res["capitalizacao"], "periodo": res["periodo"],
        "pe_atual": res["pe_atual"], "pe_medio": res["pe_medio"], "pe_mediana": res["pe_mediana"],
        "criterios": criterios, "score": score, "score_max": score_max, "veredicto": veredicto,
        "criterios_desativados": [RG.CRITERIOS_TRIAGEM[c] for c in regras["criterios_desativados"]],
        "series": series_metricas(t),
        "anual": {"anos": list(anual.index), "colunas": {c: list(anual[c]) for c in anual.columns}},
        "extras": res["extras"], "avisos": res["avisos"] + ([aviso_grupo] if aviso_grupo else []),
        "grupo": {"base": g.get("base"), "pares": g["pares"], "falhas": g["falhas"], "manual": bool(pares_manuais(t)),
                  "empresas": [{"ticker": a["ticker"], "nome": a["nome"], "score": a["score"], "score_max": a["score_max"]}
                               for a in g["analises"]]},
    }


def bloco_fundamental(t: str) -> dict:
    taxa, lim = taxa_sem_risco(), _lim()
    fe = fundamental_empresa(t, taxa, lim)
    g = _grupo(t)
    pares, com_dados, falhas = [], [], list(g.get("falhas", []))
    for s in g["pares"]:
        try:
            pares.append(fundamental_empresa(s, taxa, lim))
            com_dados.append(s)
        except Exception as e:  # noqa: BLE001
            falhas.append(f"{s}: {e}")
    valores_pares = [p["valores"] for p in pares]
    segmentos = F.avaliar(fe["valores"], valores_pares)
    med_mb, _ = F.mediana_grupo(valores_pares, "margem_bruta")

    def mediana(chave):
        for s in segmentos:
            for i in s["linhas"]:
                if i["chave"] == chave:
                    return i["mediana"]
        return float("nan")

    apoio, preco = fe["apoio"], fe["apoio"].get("preco")
    referencia = {"ok": False, "motivo": "sem cotação no Yahoo"}
    notas = W.ler_notas(t)
    manual = notas.get("consenso") or {}  # Finviz e Zacks, escritos por ti (bloqueiam leituras automáticas)
    excluidos = notas.get("metodos_excluidos") or []  # métodos que tiraste da mediana desta empresa (valores irreais)
    if isinstance(preco, (int, float)) and preco == preco and preco > 0:
        metodos = R.calcular_metodos(apoio, mediana("ev_ebitda"), mediana("pe"))
        c = consenso(t)
        linhas = metodos + R.linhas_consenso(c.get("yahoo"), c.get("stockanalysis"), c.get("erro_stockanalysis"),
                                             _positivo(manual.get("finviz")), _positivo(manual.get("zacks")), preco)
        usadas = R.aplicar_escolha(linhas, excluidos)
        referencia = {**R.consolidar(usadas, preco), "linhas": linhas, "excluidos": list(excluidos)}
    return {
        "ticker": t, "taxa_sem_risco": taxa, "premio_risco": PREMIO_RISCO, "valores": fe["valores"], "apoio": apoio,
        "notas": fe["notas"], "segmentos": segmentos, "resumo": F.resumo_quantitativo(segmentos),
        "moat": F.indicador_moat(fe["valores"], apoio, med_mb), "governanca": F.governanca(dados(t)["info"]),
        "pares": com_dados, "falhas": falhas, "referencia": referencia,
        "medianas": {k: mediana(k) for k in ("margem_bruta", "roic", "margem_ebitda", "rotacao_ativo")},
        "consenso_manual": {"finviz": manual.get("finviz"), "zacks": manual.get("zacks"), "data": manual.get("data")},
        "series": series_metricas(t),
    }


def _positivo(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0 else None


def series_metricas(t: str) -> dict:
    """Evolução anual de cada métrica (para o gráfico que aparece ao passar o cursor). Chaves = as da análise fundamental
    e as dos critérios da triagem; formato: milhoes | pct | x | num."""
    res = analise(t, _lim())
    tab, d = res["anual"], dados(t)
    datas = tab.index
    capex = M.alinhar(M.linha(d.get("cashflow"), "capex"), datas).abs()
    bruto = M.alinhar(M.linha(d.get("income"), "lucro_bruto"), datas)
    cf = d.get("cashflow")
    sbc = M.alinhar(pd.to_numeric(cf.loc["Stock Based Compensation"], errors="coerce").set_axis(M._sem_fuso(cf.columns)).dropna().sort_index()
                    if cf is not None and not cf.empty and "Stock Based Compensation" in cf.index else pd.Series(dtype=float), datas)
    div = lambda a, b: (a / b).where(b.abs() > 0)  # noqa: E731
    # Variação face ao ano anterior sobre o valor absoluto do ano anterior (um ano negativo não inverte o sinal)
    cresc = lambda s: (s - s.shift(1)) / s.shift(1).abs().where(s.shift(1).abs() > 0)  # noqa: E731
    base = {
        "receita": (tab["receita"], "milhoes", "Receita"),
        "eps": (tab["eps"], "num", "EPS diluído"),
        "ebitda": (tab["ebitda"], "milhoes", "EBITDA"),
        "fcf": (tab["fcf"], "milhoes", "Fluxo de caixa livre (FCF)"),
        "capex": (capex, "milhoes", "Capex"),
        "margem_bruta": (div(bruto, tab["receita"]), "pct", "Margem bruta"),
        "margem_liquida": (div(tab["lucro_liquido"], tab["receita"]), "pct", "Margem líquida"),
        "margem_ebitda": (tab["margem_ebitda"], "pct", "Margem EBITDA"),
        "margem_fcf": (tab["margem_fcf"], "pct", "Margem FCF"),
        "roic": (tab["roic"], "pct", "ROIC"),
        "roce": (tab["roce"], "pct", "ROCE"),
        "caixa_liquida": (-tab["divida_liquida"], "milhoes", "Caixa líquida (+) / dívida líquida (−)"),
        "nd_ebitda": (tab["divida_liq_ebitda"], "x", "Dívida líquida / EBITDA"),
        "nd_fcf": (div(tab["divida_liquida"], tab["fcf"]), "x", "Dívida líquida / FCF"),
        "cobertura": (tab["cobertura_juros"], "x", "Cobertura de juros"),
        "pe": (tab["pe_fim_exercicio"], "x", "P/E no fim do exercício"),
        "fcf_ps": (tab["fcf_por_acao"], "num", "FCF por ação"),
        "acoes": (tab["acoes"], "milhoes", "Número de ações (milhões)"),
        "crescimento": (tab["crescimento_receita"], "pct", "Crescimento da receita"),
        # Métricas de crescimento: a variação de cada ano (e não a série de onde vêm, que já tem o seu gráfico)
        "receita_cagr": (cresc(tab["receita"]), "pct", "Receita: crescimento face ao ano anterior"),
        "eps_var_total": (cresc(tab["eps"]), "pct", "EPS: variação face ao ano anterior"),
        "ebitda_cagr": (cresc(tab["ebitda"]), "pct", "EBITDA: crescimento face ao ano anterior"),
        "fcf_cagr": (cresc(tab["fcf"]), "pct", "FCF: crescimento face ao ano anterior"),
        "capex_cagr": (cresc(capex), "pct", "Capex: crescimento face ao ano anterior"),
        "var_acoes": (cresc(tab["acoes"]), "pct", "Número de ações: variação face ao ano anterior"),
        "fcf_ps_aj": (div(tab["fcf"] - sbc, tab["acoes"]), "num", "FCF por ação, líquido do SBC"),
        "sbc_fcf": (div(sbc, tab["fcf"]), "pct", "SBC em percentagem do FCF"),
    }
    # Critérios da triagem com o mesmo significado de uma métrica acima usam o mesmo gráfico
    aliases = {"roic_ultimo": "roic", "roce_ultimo": "roce", "nd_ebitda_atual": "nd_ebitda", "cobertura_atual": "cobertura", "pe_atual": "pe"}
    series = {k: {"valores": list(s), "formato": f, "rotulo": r} for k, (s, f, r) in base.items()}
    for a, k in aliases.items():
        series[a] = series[k]
    return {"anos": [f"{x:%Y}" for x in datas], "series": series}


def bloco_tecnica(t: str) -> dict:
    h = historico(t)
    p = h["precos"]
    r = T.resumo(p["Close"].tolist(), p["High"].tolist(), p["Low"].tolist())
    return {
        "ticker": t, "moeda": h["moeda"],
        "barras": [{"t": d.strftime("%Y-%m-%d"), "a": o, "max": hi, "min": lo, "f": c, "v": v}
                   for d, o, hi, lo, c, v in zip(p.index, p["Open"], p["High"], p["Low"], p["Close"], p["Volume"])],
        "mm20": r["mm20"], "mm50": r["mm50"], "mm200": r["mm200"], "rsi": r["rsi"], "macd": r["macd"],
        "ultimo": r["ultimo"], "niveis": r["niveis"], "preco": r["preco"], "tendencia": T.tendencia(r["ultimo"], r["preco"]),
    }


TERMOS_CREDIBILIDADE = ["guidance", "cumpriu", "cumpre o que", "promessa", "prometeu", "meta ", "metas", "objetivo", "superou"]


def bloco_qualitativa(t: str, fund: dict | None = None) -> dict:
    info = dados(t)["info"]
    indice = W.construir_indice()
    notas = W.ler_notas(t)
    if fund is None:
        try:
            fund = bloco_fundamental(t)
        except Exception:  # noqa: BLE001  (sem fundamental, a alocação de capital e o moat quantitativo ficam sem dados)
            fund = None
    ger = Q.dirigentes(info)
    apelidos = {k: Q.apelido(p["nome"]) for k, p in ger.items() if p}
    evid_pessoas = W.evidencias(indice, t, {k: [a] for k, a in apelidos.items()})
    evid = W.evidencias(indice, t, {"credibilidade": TERMOS_CREDIBILIDADE, "historial": list(set(apelidos.values())) or ["__nada__"]})
    evid_moat = W.evidencias(indice, t, Q.termos_moat())
    tx = Q.resumo_transacoes(transacoes_insiders(t))
    valores = fund["valores"] if fund else None
    gestao = Q.grelha_gestao(info, tx, valores, evid, notas.get("gestao"))

    pessoas = []
    for papel, rotulo in (("presidente", "Presidente do conselho"), ("ceo", "CEO"), ("cfo", "CFO")):
        p = ger.get(papel)
        if not p:
            pessoas.append({"papel": rotulo, "pessoa": None})
            continue
        ap = apelidos[papel].upper()
        mov = [{"insider": n, **v} for n, v in tx["por_pessoa"].items() if ap in n.upper()]
        pessoas.append({"papel": rotulo, "pessoa": p, "mencoes": evid_pessoas.get(papel, {}).get("mencoes", 0),
                        "excertos": evid_pessoas.get(papel, {}).get("excertos", []), "transacoes": mov})
    conhecimento = W.conhecimento(indice, t)
    try:
        series = MO.series_anuais(dados(t), analise(t, _lim())["anual"])
    except Exception:  # noqa: BLE001
        series = None
    historico_fosso = [h["fosso"] for h in conhecimento.get("historico", []) if h.get("fosso") is not None]
    moat = MO.avaliar(info, series, valores, (fund or {}).get("medianas", {}), evid_moat, conhecimento.get("fosso"),
                      historico_fosso, notas.get("moat"))
    return {"ticker": t, "pessoas": pessoas, "gestao": gestao, "transacoes": tx, "evidencias": evid,
            "governanca": F.governanca(info), "moat": moat, "industria": info.get("industry"), "setor": info.get("sector"),
            "negocio": negocio(t), "notas": notas}


def negocio(t: str) -> dict:
    """Descrição do negócio (Yahoo), traduzida para pt-PT; se a tradução falhar, fica o original."""
    info = dados(t)["info"]
    original = info.get("longBusinessSummary") or ""
    pt = TR.traduzir(original) if original else ""
    return {"texto": pt or original, "original": original, "traduzido": bool(pt) and pt != original,
            "site": info.get("website"), "funcionarios": info.get("fullTimeEmployees"), "pais": info.get("country"),
            "setor": info.get("sector"), "industria": info.get("industry")}


def _tentar(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def cotacao(tec: dict | None, moeda_fin: str | None) -> dict | None:
    """Último fecho, variação face ao fecho anterior e intervalo de 52 semanas (preços diários do Yahoo)."""
    b = (tec or {}).get("barras") or []
    if len(b) < 2:
        return None
    ult, ant = b[-1], b[-2]
    ano = b[-252:]
    return {"preco": ult["f"], "data": ult["t"], "moeda": (tec or {}).get("moeda") or moeda_fin,
            "variacao": ult["f"] / ant["f"] - 1 if ant["f"] else None, "variacao_abs": ult["f"] - ant["f"],
            "max_52s": max(x["max"] for x in ano), "min_52s": min(x["min"] for x in ano)}


COMPRA = {"comprar", "acumular"}
VENDA = {"reduzir", "vender", "evitar"}


def alertas_wiki(d: dict, conhecimento: dict, preco, comp: dict | None = None) -> list[str]:
    """Confronta o sinal com o que escreveste nos relatórios. Não muda a pontuação: é para leres antes de decidir."""
    alertas = [f"Relatório mais recente: {a}" for a in ((conhecimento.get("ficha") or {}).get("avisos") or [])]
    if (conhecimento.get("baseline") or {}).get("estado") == "nao_reconhecido":
        alertas.append("Formato de baseline não reconhecido no relatório mais recente (linhas «Filtros:» / «Cenários:» fora do "
                       "modelo): vê o separador Wiki.")
    num = lambda v: f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")  # noqa: E731
    vi = (comp or {}).get("valor_relatorio") or {}
    if (comp or {}).get("divergente"):
        alertas.append(f"O valor intrínseco da app ({num(comp['valor_app'])}) está {abs(comp['diferenca']) * 100:.0f}% "
                       f"{'acima' if comp['diferenca'] > 0 else 'abaixo'} do teu ({num(vi['na_moeda'])}, {vi.get('titulo')}). "
                       "A app usa pressupostos genéricos; vê a comparação «Relatório contra app».")
    if vi and vi.get("do_ultimo") is False:
        alertas.append(f"O teu valor intrínseco vem de um relatório anterior ({vi.get('data')}): o mais recente não o indica.")
    rec = (conhecimento.get("recomendacao") or {})
    palavras = set(str(rec.get("valor") or "").lower().replace("/", " ").replace("·", " ").split())
    if d["nivel"] in COMPRA and palavras & VENDA:
        alertas.append(f"O sinal é {d['rotulo']}, mas o teu relatório mais recente com recomendação ({rec.get('titulo')}) diz "
                       f"{rec.get('valor')}.")
    if d["nivel"] in VENDA and palavras & COMPRA and not palavras & VENDA:
        alertas.append(f"O sinal é {d['rotulo']}, mas o teu relatório mais recente com recomendação diz {rec.get('valor')}.")
    alvo = ((comp or {}).get("preco_alvo") or {}).get("na_moeda") if comp else (conhecimento.get("preco_alvo") or {}).get("valor")
    if alvo and preco and d["nivel"] in COMPRA and preco > alvo * 1.10:
        alertas.append(f"A cotação ({num(preco)}) está {(preco / alvo - 1) * 100:.0f}% acima do teu preço-alvo de {num(alvo)} "
                       f"({(conhecimento.get('preco_alvo') or {}).get('titulo')}). Confirma o valor no excerto da wiki.")
    return alertas


LIMIAR_DIVERGENCIA = 0.25  # diferença entre o valor da app e o teu a partir da qual há alerta


def _na_moeda(item: dict | None, moeda_cotacao: str | None, preco) -> dict | None:
    """
    Valor de um relatório na moeda da cotação (ex.: relatório em CAD, ação a cotar em USD), em «na_moeda». Recusa
    conversões impossíveis e valores incompatíveis com a cotação (fora de 0,2x a 5x: quase de certeza moeda ou leitura
    errada). Só quando houve conversão de moeda é que traz «cambio» {de, para, taxa, fonte}, com a taxa do Yahoo (nunca
    inventada). O campo «convertido» do item fica como veio da wiki (só o fosso x/5 → x/10 é «convertido»).
    """
    if not item or not isinstance(item.get("valor"), (int, float)):
        return None
    v, m = float(item["valor"]), item.get("moeda")
    nota, cambio = None, None
    if m and moeda_cotacao and m != moeda_cotacao:
        de, para = DAD.obter_taxa_cambio(m), DAD.obter_taxa_cambio(moeda_cotacao)
        if not (de and para):
            return {**item, "na_moeda": None, "cambio": None, "nota": f"sem câmbio {m}→{moeda_cotacao} no Yahoo: não convertido"}
        taxa = de / para
        cambio = {"de": m, "para": moeda_cotacao, "taxa": taxa,
                  "fonte": f"Yahoo, último fecho de {m}USD=X" + (f" e {moeda_cotacao}USD=X" if moeda_cotacao != "USD" else "")}
        v = v * taxa
        nota = (f"convertido de {item['valor']:.2f} {m} para {moeda_cotacao} ao câmbio {taxa:.4f} ({cambio['fonte']})"
                .replace(".", ","))
    if preco and not (0.2 <= v / preco <= 5):
        return {**item, "na_moeda": None, "cambio": cambio, "nota": "incompatível com a cotação (moeda ou leitura duvidosa)"}
    return {**item, "na_moeda": v, "cambio": cambio, "nota": nota}


def comparar_com_relatorio(d: dict, ref: dict, conhecimento: dict, preco, moeda_cotacao) -> dict:
    """O que a app calcula lado a lado com o que escreveste no relatório mais recente."""
    vi = _na_moeda(conhecimento.get("valor_intrinseco"), moeda_cotacao, preco)
    alvo = _na_moeda(conhecimento.get("preco_alvo"), moeda_cotacao, preco)
    rec = conhecimento.get("recomendacao") or {}
    app = ref.get("mediana") if ref.get("ok") else None
    teu = (vi or {}).get("na_moeda")
    dif = app / teu - 1 if app and teu else None
    return {
        "valor_app": app, "valor_relatorio": vi, "preco_alvo": alvo, "preco": preco, "moeda": moeda_cotacao,
        "diferenca": dif, "divergente": dif is not None and abs(dif) > LIMIAR_DIVERGENCIA, "limiar": LIMIAR_DIVERGENCIA,
        "sinal_app": d["nivel"], "rotulo_app": d["rotulo"], "rec_relatorio": rec.get("valor"), "rec_nivel": rec.get("nivel"),
        "rec_data": rec.get("data"), "sinal_difere": bool(rec.get("nivel")) and rec.get("nivel") != d["nivel"],
        "ancora": RG.ler()["valor_ancora"],
    }


# ---------------------------------------------------------------------------
# Preço com origem e hora, valor que conta no pilar «Preço face ao valor» e verificação das regras do Cowork
# (funções puras: testadas sem rede em testes/teste_regras_cowork.py)
# ---------------------------------------------------------------------------
MINUTOS_PRECO_ATUAL = 20  # um último negócio com mais do que isto já não é «preço atual»: conta como último fecho
FUSO = "Europe/Lisbon"
ROTULO_APP = "valor de referência da app (inclui preços-alvo de analistas)"


def escolher_preco(minuto: dict | None, cot: dict | None, agora: datetime) -> dict | None:
    """
    O preço que conta na decisão, com a origem e a hora (hora de Lisboa):
      - «preço atual»: o último minuto negociado no Yahoo, se tiver no máximo MINUTOS_PRECO_ATUAL minutos;
      - «último fecho»: o fecho diário mais recente (com a hora do último negócio desse dia, quando o Yahoo a dá).
    Nunca é o preço escrito no relatório. None = sem preço.
    """
    def local(ts):
        ts = pd.Timestamp(ts)
        return (ts.tz_localize("UTC") if ts.tz is None else ts).tz_convert(FUSO)

    def saida(preco, moeda, origem, momento=None, data_iso=None):
        data_iso = momento.date().isoformat() if momento is not None else data_iso
        hora = momento.strftime("%H:%M") if momento is not None else None
        quando = f"{data_iso} {hora}" if hora else data_iso
        dpt = "/".join(reversed(data_iso.split("-"))) if data_iso else "data desconhecida"
        texto = f"{origem} · {dpt}" + (f" {hora} (hora de Lisboa)" if hora else "")
        return {"preco": preco, "moeda": moeda, "origem": origem, "data": data_iso, "hora": hora, "quando": quando,
                "texto": texto}

    m = local(minuto["momento"]) if minuto and minuto.get("preco") else None
    if m is not None and (pd.Timestamp(agora) - m).total_seconds() <= MINUTOS_PRECO_ATUAL * 60:
        return saida(minuto["preco"], minuto.get("moeda"), "preço atual", momento=m)
    if cot and cot.get("preco"):
        hora_do_dia = m if m is not None and pd.Timestamp(minuto["momento"]).date().isoformat() == cot.get("data") else None
        return saida(cot["preco"], cot.get("moeda"), "último fecho", momento=hora_do_dia, data_iso=cot.get("data"))
    if m is not None:
        return saida(minuto["preco"], minuto.get("moeda"), "último fecho", momento=m)
    return None


SEM_RELATORIO = "sem relatório; indicativo"
SEM_CENARIOS = "o relatório não traz cenários"


def escolher_baseline(conhecimento: dict) -> dict:
    """
    O baseline com «Cenários:» válido mais recente. Se o relatório mais recente não os traz (ausentes, em lacuna, fora
    do modelo, sem moeda, probabilidades que não somam 100%), recua ao relatório anterior que os tenha, marcado e datado.
    Devolve {"baseline", "recuo", "nota", "relatorio", "titulo", "data", "motivo"} (baseline None = nenhum serve).
    """
    lista = conhecimento.get("baselines")
    if lista is None:  # forma antiga (só o relatório mais recente)
        lista = [{**conhecimento["baseline"], "do_ultimo": True}] if conhecimento.get("baseline") else []
    recente = lista[0] if lista else {}
    for i, b in enumerate(lista):
        if b.get("estado") == "nao_reconhecido" or RC.cenarios_validos(b.get("cenarios")):
            continue
        recuo = i > 0 or b.get("do_ultimo") is False
        nota = None
        if recuo:
            porque = ("tem a linha «Cenários:» num formato não reconhecido" if recente.get("estado") == "nao_reconhecido"
                      else "não traz cenários")
            nota = (f"cenários do relatório anterior ({b.get('titulo') or b.get('relatorio')}, de {_data_pt(b.get('data'))}): "
                    f"o mais recente ({_data_pt(recente.get('data'))}) {porque}")
        # data dos cenários: o campo opcional «data DD/MM/AAAA» da linha «Cenários:»; sem ele, a data do relatório
        return {"baseline": b, "recuo": recuo, "nota": nota, "relatorio": b.get("relatorio"), "titulo": b.get("titulo"),
                "data": (b.get("cenarios") or {}).get("data") or b.get("data"), "data_relatorio": b.get("data"), "motivo": None}
    motivo = ("formato de baseline não reconhecido no relatório mais recente" if recente.get("estado") == "nao_reconhecido"
              else SEM_CENARIOS)
    return {"baseline": None, "recuo": False, "nota": None, "relatorio": None, "titulo": None, "data": None, "motivo": motivo}


def posicao_efetiva(lista: str | None, correcao: dict | None) -> dict:
    """Com ou sem posição: a correção à mão (guardada nas notas) manda sobre a lista; carteira = com posição;
    em observação = sem posição; fora das listas = sem posição (e di-lo)."""
    da_lista = {"carteira": (True, "lista Carteira"), "observacao": (False, "lista Em observação")}.get(lista, (False, "fora das listas"))
    if correcao and correcao.get("com_posicao") in (True, False):
        com = correcao["com_posicao"]
        return {"com_posicao": com, "origem": "correcao", "data": correcao.get("data"), "lista": lista,
                "texto": f"{'com' if com else 'sem'} posição — correção à mão de {_data_pt(correcao.get('data'))} "
                         f"(a lista diz: {da_lista[1]}, {'com' if da_lista[0] else 'sem'} posição)"}
    return {"com_posicao": da_lista[0], "origem": "lista" if lista in ("carteira", "observacao") else "fora", "data": None,
            "lista": lista, "texto": f"{'com' if da_lista[0] else 'sem'} posição — {da_lista[1]}"}


def valor_que_conta(ancora: str, conhecimento: dict, ref: dict, preco, moeda: str | None) -> dict:
    """
    Que valor entra no pilar «Preço face ao valor»:
      - âncora «relatorio» e «Cenários:» válido (no relatório mais recente ou, recuando, num anterior, marcado e datado):
        o VALOR PONDERADO (soma de probabilidade × valor), recalculado; o cenário base da Ficha é só informação;
      - âncora «relatorio», com relatórios mas nenhum com «Cenários:» válido (ou noutra moeda): nenhum —
        «não verificável (o relatório não traz cenários)», e o valor da app aparece só como referência;
      - sem nenhum relatório: o valor de referência da app, «sem relatório; indicativo»;
      - âncora «app»: o valor de referência da app (inclui preços-alvo de analistas).
    """
    app = ref.get("mediana") if ref.get("ok") else None
    base = {"referencia_app": app, "rotulo_app": ROTULO_APP, "cenarios": None, "recuo": False, "nota": None,
            "data_cenarios": None}

    def da_app(motivo, extra=""):
        return {**base, "valor": app, "origem": "app" if app else None, "rotulo": f"do {ROTULO_APP}{extra}", "motivo": motivo,
                "leitura_sem_valor": f"sem {ROTULO_APP}" + (f" ({ref.get('motivo')})" if ref.get("motivo") else "")}

    if ancora != "relatorio":
        return da_app("escolheste o valor da app nas regras")
    if not conhecimento.get("n_relatorios"):
        return da_app(SEM_RELATORIO, f", {SEM_RELATORIO}")

    ref_txt = f" — {ROTULO_APP}: {app:.2f}, só como referência".replace(".", ",", 1) if app else ""

    def sem(motivo):
        return {**base, "valor": None, "origem": None, "rotulo": None, "motivo": motivo,
                "leitura_sem_valor": f"não verificável ({motivo}){ref_txt}"}

    esc = escolher_baseline(conhecimento)
    if not esc["baseline"]:
        return sem(esc["motivo"])
    c = esc["baseline"]["cenarios"]
    if moeda and c["moeda"] != moeda:
        return sem(f"cenários em {c['moeda']} e preço em {moeda}")
    ok_preco = isinstance(preco, (int, float)) and preco > 0
    cen = RC.calcular_cenarios(c["bear"], c["base"], c["bull"], preco if ok_preco else float("nan"))
    pond = cen["ponderado"]
    anterior = ", relatório anterior" if esc["recuo"] else ""
    return {**base, "valor": pond, "origem": "ponderado", "cenarios": cen, "recuo": esc["recuo"], "nota": esc["nota"],
            "data_cenarios": esc["data"],
            "rotulo": f"do valor ponderado do relatório ({pond:.2f} {c['moeda']}, cenários de {_data_pt(esc['data'])}{anterior})".replace(".", ",", 1),
            "motivo": None, "leitura_sem_valor": None}


def verificacao_cowork(conhecimento: dict, preco, moeda: str | None, posicao: dict, tese: str | None,
                       quando_preco: str | None, hoje: str | None = None) -> dict:
    """Regras do Cowork (regras_cowork.verificar) com o PREÇO ATUAL e a posição efetiva: veredicto, filtros
    (passa / falha / não verificável), preços de entrada, aviso de divergência e as duas datas."""
    hoje = hoje or date.today().isoformat()
    extra = {"posicao": posicao, "recuo": False, "nota_cenarios": None}
    if not conhecimento.get("n_relatorios"):
        return {**extra, "estado": "nao_verificavel", "motivo": SEM_RELATORIO, "faltam": [], "data_relatorio": None,
                "data_preco": quando_preco, "datas": RC.texto_datas(None, quando_preco, hoje)}
    esc = escolher_baseline(conhecimento)
    if not esc["baseline"]:
        return {**extra, "estado": "nao_verificavel", "motivo": esc["motivo"], "faltam": ["linha Cenários"],
                "data_relatorio": None, "data_preco": quando_preco, "datas": RC.texto_datas(None, quando_preco, hoje)}
    rec = (conhecimento.get("recomendacao") or {}).get("nivel")
    v = RC.verificar(esc["baseline"], rec, preco, moeda, posicao["com_posicao"], tese, esc["data"], quando_preco, hoje)
    return {**v, **extra, "recuo": esc["recuo"], "nota_cenarios": esc["nota"], "relatorio_cenarios": esc["relatorio"]}


def bloco_decisao(t: str) -> dict:
    erros, partes = {}, {}
    for nome, fn in (("triagem", bloco_triagem), ("fundamental", bloco_fundamental), ("tecnica", bloco_tecnica)):
        try:
            partes[nome] = fn(t)
        except Exception as e:  # noqa: BLE001
            erros[nome] = f"{type(e).__name__}: {e}"
    try:
        partes["qualitativa"] = bloco_qualitativa(t, partes.get("fundamental"))
    except Exception as e:  # noqa: BLE001
        erros["qualitativa"] = f"{type(e).__name__}: {e}"
    if "triagem" in erros and "tecnica" in erros:
        raise HTTPException(404, f"Sem dados no Yahoo para '{t}'. Confirma o ticker: os europeus levam o sufixo da bolsa "
                                 "(ASML.AS, SAP.DE, MC.PA, NOVO-B.CO, SHEL.L).")
    tri, fun, tec, qual = (partes.get(k) for k in ("triagem", "fundamental", "tecnica", "qualitativa"))
    ref = (fun or {}).get("referencia") or {}
    indice = W.construir_indice()
    conhecimento = W.conhecimento(indice, t)
    tese = (conhecimento.get("tese") or {}).get("valor")
    cot = cotacao(tec, (tri or {}).get("moeda"))
    moeda_cot = (cot or {}).get("moeda") or (tri or {}).get("moeda")
    # Preço: o atual do Yahoo (ou o último fecho), com origem e hora; nunca o que vem escrito no relatório
    preco_info = escolher_preco(_tentar(preco_minuto, t), cot, datetime.now(timezone.utc))
    if preco_info is None and ref.get("ok") and ref.get("preco"):
        preco_info = {"preco": ref["preco"], "moeda": moeda_cot, "origem": "cotação do Yahoo", "data": None, "hora": None,
                      "quando": None, "texto": "cotação do Yahoo · hora não disponível"}
    preco = (preco_info or {}).get("preco")
    lista = L.onde_esta(L.ler(indice), t)
    posicao = posicao_efetiva(lista, W.ler_notas(t).get("posicao"))
    regras_atuais = RG.ler()
    vq = valor_que_conta(regras_atuais["valor_ancora"], conhecimento, ref, preco, moeda_cot)
    verificacao = verificacao_cowork(conhecimento, preco, moeda_cot, posicao, tese, (preco_info or {}).get("quando"))
    d = DEC.calcular(
        qualidade=(tri or {}).get("veredicto", {}).get("nivel"),
        preco=preco,
        mediana_valor=vq["valor"],
        rotulo_valor=vq["rotulo"] or "do valor",
        leitura_sem_valor=vq["leitura_sem_valor"] or "sem valor que conte",
        tendencia=(tec or {}).get("tendencia", "nd"),
        rsi=((tec or {}).get("ultimo") or {}).get("rsi", float("nan")),
        nota_gestao=((qual or {}).get("gestao") or {}).get("nota"),
        moat=((qual or {}).get("moat") or {}).get("classe"),
        moat_tendencia=((qual or {}).get("moat") or {}).get("tendencia"),
        tese=tese,
        desativados=regras_atuais["pilares_desativados"],
    )
    d = DEC.alinhar_cowork(d, verificacao)  # o sinal final segue as regras do Cowork (ver decisao.alinhar_cowork)
    comp = comparar_com_relatorio(d, ref, conhecimento, preco, moeda_cot)
    comp["valor_conta"] = {k: vq[k] for k in ("valor", "origem", "rotulo", "motivo", "referencia_app", "rotulo_app", "recuo",
                                              "nota", "data_cenarios")}
    alvo = (comp["preco_alvo"] or {}).get("na_moeda")
    alertas = alertas_wiki(d, conhecimento, preco, comp)
    if verificacao.get("aviso"):
        alertas.insert(0, verificacao["aviso"])
    if vq.get("nota"):
        alertas.append(f"Valor que conta: {vq['nota']}.")
    alertas += [f"Contas do relatório: {x}." for x in verificacao.get("avisos_contas") or []]
    mediana_app = ref.get("mediana") if ref.get("ok") else None
    return {
        "alertas": alertas,
        "comparacao": comp,
        "verificacao": verificacao,
        "ticker": t, "data": date.today().isoformat(), "nome": (tri or {}).get("nome"), "preco": preco,
        "preco_info": preco_info,
        "negocio": (qual or {}).get("negocio"), "lista": lista, "posicao": posicao,
        "cotacao": cot,
        "calendario": _tentar(bloco_calendario, t),
        "moeda": (tri or {}).get("moeda"), "decisao": d, "erros": erros,
        "resumo": {
            "triagem": (tri or {}).get("veredicto"), "score": (tri or {}).get("score"), "score_max": (tri or {}).get("score_max"),
            # face_mediana (mediana ÷ preço − 1) fica só por compatibilidade com a app Carteira; a Decisão usa o desconto
            "valor": {**{k: ref.get(k) for k in ("ok", "mediana", "n", "entradas", "face_mediana", "motivo")},
                      "desconto": 1 - preco / mediana_app if mediana_app and preco else None},
            "tecnica": {"tendencia": (tec or {}).get("tendencia"), **((tec or {}).get("ultimo") or {})},
            "gestao": {k: ((qual or {}).get("gestao") or {}).get(k) for k in ("nota", "classe", "n_avaliados", "n_total")},
            "moat": {k: ((qual or {}).get("moat") or {}).get(k) for k in ("nota", "classe", "tipos_confirmados", "tendencia")},
            "moat_tipos_sugeridos": [x["nome"] for x in ((qual or {}).get("moat") or {}).get("tipos", []) if x["sugerido"]],
        },
        "wiki": {
            "tese": conhecimento.get("tese"), "recomendacao": conhecimento.get("recomendacao"),
            "preco_alvo": conhecimento.get("preco_alvo"), "fosso": conhecimento.get("fosso"),
            "valor_intrinseco": conhecimento.get("valor_intrinseco"),
            "formato": conhecimento.get("formato"), "ficha": conhecimento.get("ficha"), "baseline": conhecimento.get("baseline"),
            "face_alvo": (preco / alvo - 1) if alvo and preco else None,  # só por compatibilidade com a app Carteira
            "desconto_alvo": (1 - preco / alvo) if alvo and preco else None, "n_relatorios": conhecimento.get("n_relatorios"),
            # o separador Wiki só aparece quando há alguma coisa para ler: relatórios, análises guardadas ou notas
            "n_analises": len(_tentar(W.analises_guardadas, t) or []),
            "tem_notas": bool(((_tentar(W.ler_notas, t) or {}).get("texto") or "").strip()),
        },
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _executar(fn, *args):
    try:
        return limpo(fn(*args))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _falha(e)


@em_cache(1)
def _procurar(q: str):
    return DAD.procurar_tickers(q)


@app.get("/api/procurar")
def api_procurar(q: str = ""):
    """Sugestões de tickers enquanto se escreve na caixa do topo (Yahoo, como a pesquisa da Carteira)."""
    try:
        return _procurar(q.strip().lower())
    except Exception:  # noqa: BLE001 — sem rede, a caixa continua a aceitar o ticker escrito à mão
        return []


@app.get("/api/empresa/{ticker}/compras")
def api_compras(ticker: str, moeda: str = ""):
    """As tuas compras e vendas desta cotação (Carteira de Ativos), na moeda do gráfico, para o botão «Compras» da Técnica."""
    return limpo(CP.compras_para_grafico(CP.ler_carteira(), _t(ticker), moeda or None))


_cache_cot: dict = {}


@app.get("/api/cotacoes")
def api_cotacoes(t: str = ""):
    """Preço e variação do dia de até 8 tickers (para as sugestões), em paralelo e guardados 5 minutos."""
    from concurrent.futures import ThreadPoolExecutor

    tickers = [x.strip().upper() for x in t.split(",") if x.strip()][:8]

    def um(x):
        hit = _cache_cot.get(x)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        try:
            v = DAD.cotacao_rapida(x)
        except Exception:  # noqa: BLE001 — sem preço, a sugestão aparece na mesma
            v = {"ticker": x, "preco": None, "moeda": None, "variacao": None}
        _cache_cot[x] = (time.time(), v)
        return v

    with ThreadPoolExecutor(max_workers=8) as ex:
        return limpo(list(ex.map(um, tickers)))


@app.get("/api/indice")
def api_indice():
    return limpo(W.indice_leve(W.construir_indice()))


@app.get("/api/pesquisa")
def api_pesquisa(q: str = "", veredito: str | None = None, ocultos: bool = False):
    return limpo(W.pesquisar(W.construir_indice(), q, veredito, ocultos))


@app.get("/api/wiki/{ticker}")
def api_wiki(ticker: str):
    t = _t(ticker)
    indice = W.construir_indice()
    k = W.conhecimento(indice, t)
    rels = [{kk: vv for kk, vv in r.items() if kk != "texto"} for r in W.relatorios_da_empresa(indice, k["empresa"])]
    return limpo({"ticker": t, "conhecimento": k, "relatorios": rels, "analises": W.analises_guardadas(t), "notas": W.ler_notas(t),
                  "noticias": N.lista(t, 8), "n_noticias": len(N.ler(t)["noticias"])})


@app.get("/api/empresa/{ticker}/triagem")
def api_triagem(ticker: str):
    return _executar(bloco_triagem, _t(ticker))


@app.get("/api/empresa/{ticker}/fundamental")
def api_fundamental(ticker: str):
    return _executar(bloco_fundamental, _t(ticker))


@app.get("/api/empresa/{ticker}/tecnica")
def api_tecnica(ticker: str):
    return _executar(bloco_tecnica, _t(ticker))


@app.get("/api/empresa/{ticker}/qualitativa")
def api_qualitativa(ticker: str):
    return _executar(bloco_qualitativa, _t(ticker))


@app.get("/api/empresa/{ticker}/decisao")
def api_decisao(ticker: str):
    return _executar(bloco_decisao, _t(ticker))


@app.post("/api/empresa/{ticker}/guardar")
def api_guardar(ticker: str):
    t = _t(ticker)
    analise_atual = _executar(bloco_decisao, t)
    return {"ficheiro": W.guardar_analise(t, analise_atual), "analise": analise_atual}


@app.put("/api/empresa/{ticker}/posicao")
def api_posicao(ticker: str, corpo: dict = Body(...)):
    """Correção à mão da posição (manda sobre a lista): {"com_posicao": true | false}; null apaga a correção."""
    t = _t(ticker)
    v = corpo.get("com_posicao")
    if v not in (True, False, None):
        raise HTTPException(400, "com_posicao tem de ser true, false ou null")
    W.guardar_notas(t, {"posicao": None if v is None else {"com_posicao": v, "data": date.today().isoformat()}})
    return limpo(posicao_efetiva(L.onde_esta(L.ler(W.construir_indice()), t), W.ler_notas(t).get("posicao")))


@app.post("/api/validar_tickers")
def api_validar_tickers(tickers: list[str] = Body(..., embed=True)):
    """Confirma no Yahoo cada ticker antes de o gravar (ex.: concorrentes escritos à mão)."""
    return [DAD.validar_ticker(t) for t in tickers[:10] if t.strip()]


@app.put("/api/empresa/{ticker}/notas")
def api_notas(ticker: str, notas: dict = Body(...)):
    permitidas = {"gestao", "moat", "texto", "pares", "consenso", "metodos_excluidos"}
    if "metodos_excluidos" in notas:
        excl = notas["metodos_excluidos"] or []
        if not isinstance(excl, list) or any(x not in R.CHAVES_METODOS for x in excl):
            raise HTTPException(400, "metodos_excluidos tem de ser uma lista de: " + ", ".join(R.CHAVES_METODOS))
        notas["metodos_excluidos"] = sorted(set(excl))
    if "consenso" in notas:
        c = notas["consenso"] or {}
        notas["consenso"] = {"finviz": _positivo(c.get("finviz")), "zacks": _positivo(c.get("zacks")), "data": date.today().isoformat()}
    return W.guardar_notas(_t(ticker), {k: v for k, v in notas.items() if k in permitidas})


@app.get("/api/regras")
def api_regras():
    return limpo({
        "decisao": DEC.regras(), "criterios": C.carregar(), "criterios_definicao": C.DEFINICAO,
        "configuracao": RG.ler(), "criterios_triagem": RG.CRITERIOS_TRIAGEM, "pilares": RG.PILARES,
        "catalogo": RG.CATALOGO, "nomes_personalizados": {p["id"]: RG.nome_personalizado(p) for p in RG.ler()["personalizados"]},
        "pasta_dados": str(config.pasta_dados()),
    })


@app.put("/api/criterios")
def api_criterios(valores: dict = Body(...)):
    _cache.clear()
    return C.guardar(valores)


@app.put("/api/regras")
def api_regras_guardar(novas: dict = Body(...)):
    try:
        return RG.guardar(novas)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Listas: carteira e observação ──
def _listas() -> dict:
    return L.ler(W.construir_indice())


@app.get("/api/listas")
def api_listas():
    listas = _listas()
    indice = W.construir_indice()
    for k in L.LISTAS:
        for x in listas[k]:
            e = W.empresa_por_ticker(indice, x["ticker"])
            x["veredito"] = e["veredito"] if e else None
            x["ultimo_periodo"] = e["ultimo_periodo"] if e else None
            x["na_wiki"] = bool(e)
    return {"listas": listas, "nomes": L.LISTAS}


@app.post("/api/listas/{destino}")
def api_listas_mover(destino: str, corpo: dict = Body(...)):
    """Põe uma empresa na carteira, em observação ou nos outros (se tiver pasta de relatórios, move a pasta)."""
    t = _t(corpo.get("ticker", ""))
    indice = W.construir_indice()
    nome = corpo.get("nome")
    if not nome and not W.empresa_por_ticker(indice, t) and destino != "outros":
        try:  # empresa nova, sem relatórios: confirma-se no Yahoo que o ticker existe
            nome = (dados(t)["info"] or {}).get("longName")
        except Exception:  # noqa: BLE001
            raise HTTPException(404, f"Sem dados no Yahoo para '{t}'. Confirma o ticker (os europeus levam o sufixo da bolsa).")
    try:
        r = L.mover(t, destino, nome, indice)
        PD.retirar(t)  # decidido: sai de «Da Pesquisa · por decidir»
        return r
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(409, f"Não consegui mover a pasta ({e}). Fecha os ficheiros dessa empresa que tenhas abertos e tenta de novo.")


@app.delete("/api/listas/{ticker}")
def api_listas_retirar(ticker: str):
    return api_listas_mover("outros", {"ticker": ticker})


# ── Da Pesquisa, por decidir: analisadas em segundo plano, sem entrar em nenhuma lista ──
_fila_pd: list[str] = []
_fila_pd_tranca = threading.Lock()
_fila_pd_ativa = {"thread": None}


def _analisar_por_decidir():
    """Uma de cada vez (a primeira análise de uma empresa vai buscar os concorrentes e pode levar 1 a 2 minutos)."""
    while True:
        with _fila_pd_tranca:
            if not _fila_pd:
                _fila_pd_ativa["thread"] = None
                return
            t = _fila_pd.pop(0)
        if not any(x["ticker"] == t for x in PD.ler()):
            continue  # dispensada ou posta numa lista entretanto
        try:
            r = limpo(bloco_decisao(t))
            d = r.get("decisao") or {}
            PD.marcar(t, estado="analisada", nivel=d.get("nivel"), rotulo=d.get("rotulo"), preco=r.get("preco"), moeda=r.get("moeda"),
                      nome=r.get("nome") or t, analisada=datetime.now().strftime("%Y-%m-%d %H:%M"), erro=None)
        except Exception as e:  # noqa: BLE001
            PD.marcar(t, estado="erro", erro=f"{type(e).__name__}: {e}"[:300])


def _juntar_fila_pd(tickers: list[str]):
    with _fila_pd_tranca:
        for t in tickers:
            if t not in _fila_pd:
                _fila_pd.append(t)
        if not _fila_pd_ativa["thread"]:
            _fila_pd_ativa["thread"] = threading.Thread(target=_analisar_por_decidir, daemon=True)
            _fila_pd_ativa["thread"].start()


@app.get("/api/por_decidir")
def api_por_decidir():
    return limpo(PD.ler())


@app.post("/api/por_decidir")
def api_por_decidir_juntar(corpo: dict = Body(...)):
    """{"empresas": [{"ticker", "nome"}], "origem": "..."}: entram em «por decidir» e são analisadas em segundo plano."""
    listas = _listas()
    nas_listas = {x["ticker"].upper() for k in ("carteira", "observacao") for x in listas.get(k, [])}
    r = PD.juntar(corpo.get("empresas") or [], corpo.get("origem") or "Pesquisa do Centro de Investimento", nas_listas)
    _juntar_fila_pd(r["novas"])
    return r


@app.post("/api/por_decidir/{ticker}/analisar")
def api_por_decidir_reanalisar(ticker: str):
    t = _t(ticker)
    PD.marcar(t, estado="a_analisar", erro=None)
    _juntar_fila_pd([t])
    return {"ok": True}


@app.delete("/api/por_decidir/{ticker}")
def api_por_decidir_dispensar(ticker: str):
    """Dispensar: sai de «por decidir» sem entrar em nenhuma lista (as análises em memória não se perdem)."""
    if not PD.retirar(_t(ticker)):
        raise HTTPException(404, "Essa empresa não está por decidir.")
    return {"ok": True}


# As que ficaram a meio (servidor reiniciado) voltam à fila
_juntar_fila_pd([x["ticker"] for x in PD.ler() if x.get("estado") == "a_analisar"])


# ── Notícias ──
def _nome(t: str) -> str | None:
    for k in L.LISTAS:
        for x in _listas()[k]:
            if x["ticker"].upper() == t.upper():
                return x.get("nome")
    try:
        return dados(t)["info"].get("longName")
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/empresa/{ticker}/noticias")
def api_noticias(ticker: str, atualizar: bool = False):
    t = _t(ticker)
    erro = None
    try:
        N.recolher(t, _nome(t), forcar=atualizar)
    except Exception as e:  # noqa: BLE001  (sem rede: mostra-se o que já está guardado)
        erro = f"Não foi possível atualizar agora: {e}"
    arq = N.ler(t)
    return limpo({"ticker": t, "atualizado": arq.get("atualizado"), "noticias": N.lista(t), "erro": erro})


@app.get("/api/noticias/recentes")
def api_noticias_recentes(dias: int = 2):
    return limpo({"noticias": N.de_hoje(L.seguidas(_listas()), dias)})


@app.get("/api/empresa/{ticker}/calendario")
def api_calendario_empresa(ticker: str):
    return _executar(bloco_calendario, _t(ticker))


@app.get("/api/calendario")
def api_calendario():
    """Próximos resultados de todas as empresas da carteira e da observação (pedidos ao Yahoo em paralelo)."""
    from concurrent.futures import ThreadPoolExecutor

    listas = _listas()
    onde = {x["ticker"]: (k, x.get("nome")) for k in L.LISTAS for x in listas[k]}
    with ThreadPoolExecutor(max_workers=6) as ex:
        resultados = list(ex.map(lambda t: (t, _tentar(bloco_calendario, t)), onde))
    linhas = []
    for t, c in resultados:
        lista, nome = onde[t]
        linhas.append({"ticker": t, "nome": nome, "lista": lista, "proximo": (c or {}).get("proximo"),
                       "ultimo": (c or {}).get("ultimo"), "relatorio": (c or {}).get("relatorio"), "erro": c is None})
    linhas.sort(key=lambda x: ((x["proximo"] or {}).get("data") or "9999", x["ticker"]))
    return limpo({"empresas": linhas, "hoje": date.today().isoformat()})


def _lado(t: str) -> dict:
    """Tudo o que se compara de uma empresa: a decisão completa (com triagem, valor, técnica, gestão e moat) e as séries."""
    d = bloco_decisao(t)
    fe = fundamental_empresa(t, taxa_sem_risco(), _lim())
    return {"ticker": t, "nome": d.get("nome"), "moeda": d.get("moeda"), "preco": d.get("preco"), "cotacao": d.get("cotacao"),
            "decisao": d["decisao"], "resumo": d["resumo"], "valores": fe["valores"], "series": _tentar(series_metricas, t),
            "tese": (d.get("wiki") or {}).get("tese"), "erros": d.get("erros")}


def _vencedor(va, vb, melhor: str | None):
    ok = lambda v: isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf"))  # noqa: E731
    if melhor is None or not ok(va) or not ok(vb):
        return None
    if abs(va - vb) <= 1e-9 * max(abs(va), abs(vb), 1):
        return "empate"
    return "a" if (va > vb) == (melhor == "maior") else "b"


@app.get("/api/comparar/{a}/{b}")
def api_comparar(a: str, b: str):
    """Frente a frente: as mesmas métricas e valores para duas empresas, com a vencedora de cada linha."""
    ta, tb = _t(a), _t(b)
    if ta == tb:
        raise HTTPException(400, "Escolhe uma empresa diferente para comparar.")
    try:
        la, lb = _lado(ta), _lado(tb)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _falha(e)
    mesma_moeda = (la["moeda"] or "") == (lb["moeda"] or "")

    seccoes = []
    for seg, linhas in F.SEGMENTOS:
        itens = []
        for rotulo, chave, fmt, melhor, comparavel in linhas:
            va, vb = la["valores"].get(chave), lb["valores"].get(chave)
            absoluto = fmt in ("milhoes", "num") and not comparavel
            itens.append({"rotulo": rotulo, "chave": chave, "formato": fmt, "a": va, "b": vb, "melhor": melhor,
                          "vencedor": _vencedor(va, vb, melhor) if comparavel or (absoluto and mesma_moeda and melhor) else None})
        seccoes.append({"nome": seg, "linhas": itens})

    def linha(rotulo, va, vb, fmt, melhor, chave=None):
        return {"rotulo": rotulo, "chave": chave, "formato": fmt, "a": va, "b": vb, "melhor": melhor, "vencedor": _vencedor(va, vb, melhor)}

    ra, rb = la["resumo"], lb["resumo"]
    rel = lambda r: r["valor"]["mediana"] if r["valor"].get("ok") else None  # noqa: E731
    desconto = lambda lado: lado["decisao"].get("desconto_valor")  # noqa: E731  (1 − preço / valor que conta no sinal)
    nivel = {"comprar": 2, "acumular": 1, "manter": 0, "reduzir": -1, "vender": -2}
    classe_moat = {"largo": 2, "estreito": 1, "nenhum": 0}
    seccoes.insert(0, {"nome": "Síntese", "linhas": [
        linha("Sinal da decisão (pontos)", la["decisao"]["pontos"], lb["decisao"]["pontos"], "pontos", "maior"),
        linha("Nível do sinal", nivel.get(la["decisao"]["nivel"]), nivel.get(lb["decisao"]["nivel"]), "nivel", "maior"),
        linha("Triagem (pontos)", ra.get("score"), rb.get("score"), "num1", "maior"),
        linha("Desconto face ao valor que conta no sinal (positivo = preço abaixo)", desconto(la), desconto(lb), "pct", "maior"),
        linha("Valor de referência da app (inclui preços-alvo de analistas)", rel(ra), rel(rb), "preco", None),
        linha("Nota da gestão (0 a 10)", (ra.get("gestao") or {}).get("nota"), (rb.get("gestao") or {}).get("nota"), "num1", "maior"),
        linha("Força do moat (0 a 10)", (ra.get("moat") or {}).get("nota"), (rb.get("moat") or {}).get("nota"), "num1", "maior"),
        linha("Classe do moat", classe_moat.get((ra.get("moat") or {}).get("classe")), classe_moat.get((rb.get("moat") or {}).get("classe")), "moat", "maior"),
        linha("RSI (14)", (ra.get("tecnica") or {}).get("rsi"), (rb.get("tecnica") or {}).get("rsi"), "num1", None),
    ]})
    contagem = {"a": 0, "b": 0, "empate": 0}
    for s in seccoes[1:]:
        for i in s["linhas"]:
            if i["vencedor"]:
                contagem[i["vencedor"]] += 1
    return limpo({"a": la, "b": lb, "seccoes": seccoes, "contagem": contagem, "mesma_moeda": mesma_moeda})


PEDIDOS = "pedidos_relatorio.json"


def _data_pt(iso: str | None) -> str:
    return "/".join(reversed(iso.split("-"))) if iso else ""


@app.get("/api/relatorios-em-falta")
def api_relatorios_em_falta():
    """Empresas da carteira e da observação sem relatório teu sobre os últimos resultados (ou sem relatório nenhum),
    com o tipo de relatório a pedir ao Cowork e onde o gravar para a app o ler sozinha."""
    from concurrent.futures import ThreadPoolExecutor

    listas, indice = _listas(), W.construir_indice()
    pedidos = config.ler_json(PEDIDOS, {})
    pasta_lista = {k: (W.pasta_da_lista(k) + "\\") if W.pasta_da_lista(k) else "" for k in L.LISTAS}
    itens = [(k, x) for k in L.LISTAS for x in listas[k]]
    with ThreadPoolExecutor(max_workers=6) as ex:
        cals = dict(zip([x["ticker"] for _, x in itens], ex.map(lambda kx: _tentar(bloco_calendario, kx[1]["ticker"]), itens)))
    saida = {k: [] for k in L.LISTAS}
    for lista, x in itens:
        t = x["ticker"]
        c = cals.get(t) or {}
        rel = c.get("relatorio") or {}
        if rel.get("estado") not in ("por_pedir", "sem_relatorios"):
            continue
        empresa = W.empresa_por_ticker(indice, t)
        rels = [r for r in W.relatorios_da_empresa(indice, empresa) if not r["arquivado"]]
        ultimo = max(rels, key=lambda r: r["data"], default=None)
        # Pasta onde o relatório deve ficar: a da empresa, se já existir; senão, a da lista
        if empresa and ultimo:
            pasta = W.pasta_da_empresa(ultimo["caminho"]).replace("/", "\\") + "\\"
        else:
            pasta = f"{pasta_lista[lista]}{(x.get('nome') or t).upper().split()[0]}\\"
        base = (ultimo or {}).get("ticker") or t.split(".")[0]
        tem_iniciacao = any(r["tipo"] == "iniciacao" for r in rels)
        ult_res = (c.get("ultimo") or {}).get("data")
        if rel["estado"] == "sem_relatorios":
            tipo, motivo = "Relatório de iniciação", "ainda não tens nenhum relatório sobre esta empresa"
            nome = f"{base}_iniciacao_{date.today().isoformat()}.html"
        elif not tem_iniciacao:
            tipo = "Relatório de iniciação (formato atual)"
            motivo = f"resultados de {_data_pt(ult_res)} sem relatório teu; o último é de {_data_pt(ultimo['data'])} e não há iniciação no formato atual"
            nome = f"{base}_iniciacao_{date.today().isoformat()}.html"
        else:
            tipo = "Acompanhamento trimestral"
            motivo = f"resultados de {_data_pt(ult_res)} sem relatório teu; o último é de {_data_pt(ultimo['data'])}"
            nome = f"{base}_FYAAAA-Qn_acompanhamento.html  (dentro de uma subpasta FYAAAA-Qn)"
        saida[lista].append({
            "ticker": t, "nome": x.get("nome"), "motivo": motivo, "tipo": tipo, "resultados": ult_res,
            "ultimo_relatorio": {"data": ultimo["data"], "titulo": ultimo["titulo"], "caminho": ultimo["caminho"]} if ultimo else None,
            "pasta": pasta, "ficheiro": nome, "proximos": (c.get("proximo") or {}).get("data"),
            "pedido": pedidos.get(t, {}).get("data") if pedidos.get(t, {}).get("resultados") == ult_res else None,
        })
    for k in saida:
        saida[k].sort(key=lambda i: (i["pedido"] is not None, i["resultados"] or "9999"))
    return limpo({"listas": saida, "nomes": L.LISTAS, "pasta_relatorios": str(W.pasta_relatorios())})


RE_NOME_RELATORIO = re.compile(r"^([A-Za-z0-9.]{1,8})_(iniciacao_\d{4}-\d{2}-\d{2}.*|FY\d{4}-[QH]\d_(analise|acompanhamento))\.html?$", re.I)


def _pasta_da_empresa(indice: dict, ticker: str) -> str | None:
    """Pasta (relativa à dos relatórios) onde estão os relatórios desta empresa, ou None se ainda não houver."""
    empresa = W.empresa_por_ticker(indice, ticker)
    rels = [r for r in W.relatorios_da_empresa(indice, empresa) if not r["arquivado"]]
    if not rels:
        return None
    return W.pasta_da_empresa(rels[-1]["caminho"])


@app.get("/api/transferencias")
def api_transferencias():
    """Relatórios com o nome no formato dos teus (TICKER_FYAAAA-Qn_analise.html, TICKER_iniciacao_AAAA-MM-DD.html) que estão
    nas Transferências dos últimos 30 dias e ainda não existem na pasta dos relatórios."""
    pasta = Path.home() / "Downloads"
    raiz = W.pasta_relatorios()
    existentes = {p.name.lower() for p in W._html_da_pasta(raiz)}
    indice, listas = W.construir_indice(), _listas()
    limite = time.time() - 30 * 86400
    saida = []
    for f in sorted(pasta.glob("*.htm*"), key=lambda p: p.stat().st_mtime, reverse=True) if pasta.exists() else []:
        m = RE_NOME_RELATORIO.match(f.name)
        if not m or f.stat().st_mtime < limite or f.name.lower() in existentes:
            continue
        ticker = m.group(1).upper()
        tk_yahoo = next((x["ticker"] for k in L.LISTAS for x in listas[k] if x["ticker"].split(".")[0] == ticker), ticker)
        base = _pasta_da_empresa(indice, tk_yahoo)
        if not base:
            p = W.pasta_da_lista("carteira" if L.onde_esta(listas, tk_yahoo) == "carteira" else "observacao")
            base = f"{p}/{ticker}" if p else ticker
        periodo = re.search(r"FY\d{4}-[QH]\d", f.name, re.I)
        destino = f"{base}/{periodo.group(0).upper()}" if periodo else base
        saida.append({"ficheiro": f.name, "ticker": tk_yahoo, "data": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes"),
                      "destino": destino.replace("/", "\\") + "\\"})
    return {"ficheiros": saida, "pasta": str(pasta)}


@app.post("/api/transferencias/arquivar")
def api_arquivar(corpo: dict = Body(...)):
    """Copia um relatório das Transferências para a pasta dos relatórios (não apaga o original)."""
    import shutil

    nome = Path(str(corpo.get("ficheiro", ""))).name
    origem = Path.home() / "Downloads" / nome
    raiz = W.pasta_relatorios().resolve()
    destino = (raiz / str(corpo.get("destino", "")).replace("\\", "/").strip("/") / nome).resolve()
    if not RE_NOME_RELATORIO.match(nome) or not origem.exists():
        raise HTTPException(404, "Ficheiro não encontrado nas Transferências")
    if raiz not in destino.parents:
        raise HTTPException(400, "Destino fora da pasta dos relatórios")
    if destino.exists():
        raise HTTPException(409, f"Já existe um relatório com esse nome em {destino.parent}")
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origem, destino)
    return {"copiado_para": str(destino)}


@app.put("/api/relatorios-em-falta/{ticker}")
def api_marcar_pedido(ticker: str, corpo: dict = Body(...)):
    """Marca (ou desmarca) que já pediste ao Cowork o relatório sobre os últimos resultados desta empresa."""
    t = _t(ticker)
    pedidos = config.ler_json(PEDIDOS, {})
    if corpo.get("pedido"):
        pedidos[t] = {"data": date.today().isoformat(), "resultados": corpo.get("resultados")}
    else:
        pedidos.pop(t, None)
    config.escrever_json(PEDIDOS, pedidos)
    return pedidos.get(t)


@app.get("/api/empresa/{ticker}/negocio")
def api_negocio(ticker: str):
    return _executar(negocio, _t(ticker))


_parar = threading.Event()


@app.on_event("startup")
def arrancar_noticias():
    threading.Thread(target=N.recolha_periodica, args=(lambda: L.seguidas(_listas()), _nome, _parar), daemon=True).start()


@app.on_event("startup")
def abrir_navegador():
    """O INICIAR.bat pede para abrir o navegador: faz-se aqui, já com o servidor pronto a responder."""
    endereco = os.environ.get("ABRIR_NAVEGADOR")
    if endereco:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [f"{endereco}/?v={int(time.time())}#/"]).start()


@app.on_event("shutdown")
def parar_noticias():
    _parar.set()


@app.post("/api/renovar/{ticker}")
def api_renovar(ticker: str):
    t = _t(ticker)
    for chave in [k for k in _cache if t in k[1]]:
        _cache.pop(chave, None)
    return {"ok": True}


@app.get("/relatorios/{caminho:path}")
def relatorio(caminho: str):
    raiz = W.pasta_relatorios().resolve()
    f = (raiz / caminho).resolve()
    if raiz not in f.parents or f.suffix.lower() != ".html" or not f.exists():
        raise HTTPException(404, "Relatório não encontrado")
    return FileResponse(f, media_type="text/html; charset=utf-8")


# Interface (compilada em web/dist): ficheiros estáticos e, para qualquer outra rota, a página principal
DIST = RAIZ / "web" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/favicon.png", include_in_schema=False)
def favicon():
    f = DIST / "favicon.png"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/png")


@app.get("/{resto:path}", include_in_schema=False)
def pagina(resto: str):
    if (DIST / "index.html").exists():
        # Nunca guardar em cache a página principal: assim uma versão nova da app aparece logo (os ficheiros em
        # /assets têm o conteúdo no nome e podem ficar em cache à vontade)
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return HTMLResponse("<h1>Interface por compilar</h1><p>Corre <code>npm run build</code> na pasta web.</p>", status_code=503)
