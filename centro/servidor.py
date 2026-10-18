"""
Centro de Investimento — uma só app para a Carteira, a Decisão de Investimento, a Descoberta de Ações e os pedidos ao Cowork.

Fase 1 (10/10/2026): não substitui nem altera nenhuma das apps que já funcionam.
- Carteira: a interface e os cálculos são os da própria Carteira de Ativos (lidos de Documents\\carteira_ativos\\apps-script),
  com os dados exportados pela Carteira para a pasta partilhada do Google Drive (carteira_dados.json). Só leitura.
- Decisão de Investimento: lida pela API dela (localhost:8600) e pela sincronização já feita para a Carteira.
- Descoberta de Ações: lê os resultados guardados (resultados\\*.pkl), sem correr nada.
- Cowork: os pedidos de relatório ficam em ficheiros JSON em Estudos Empresas-Relatorios\\_PEDIDOS.
Tudo gratuito: Yahoo Finance, BCE, Google Drive para computador.

Arrancar: INICIAR.bat (abre http://localhost:8700).
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------------------------------------------
# Caminhos e constantes (num só sítio)
# ---------------------------------------------------------------------------------------------------------------
AQUI = Path(__file__).resolve().parent
CASA = Path.home()
# No PC valem os caminhos de sempre; na nuvem (trabalhador do GitHub) vêm das variáveis CENTRO_*
_env = os.environ.get
CARTEIRA_APP = Path(_env("CENTRO_CARTEIRA_APP") or CASA / "Documents" / "carteira_ativos")
CARTEIRA_UI = CARTEIRA_APP / "apps-script"
PASTA_DRIVE = Path(_env("CENTRO_PASTA_DRIVE") or r"G:\O meu disco\Carteira - sincronização")
EXPORT_CARTEIRA = PASTA_DRIVE / "carteira_dados.json"
if os.environ.get("CENTRO_DADOS_TESTE"):  # só para testes: outro ficheiro no lugar da exportação real
    EXPORT_CARTEIRA = Path(os.environ["CENTRO_DADOS_TESTE"])
SYNC_DECISAO = PASTA_DRIVE / "sincronizacao_carteira.json"
TRIAGENS = PASTA_DRIVE / "triagens_carteira.json"
RELATORIOS = Path(_env("CENTRO_RELATORIOS") or CASA / "OneDrive" / "Ambiente de Trabalho" / "Estudos Empresas-Relatorios")
DESCOBERTA = Path(_env("CENTRO_DESCOBERTA") or RELATORIOS / "APP" / "triagem_acoes" / "resultados")
PEDIDOS_COWORK = Path(_env("CENTRO_PEDIDOS_COWORK") or RELATORIOS / "_PEDIDOS")
DECISAO = _env("CENTRO_DECISAO_URL") or "http://127.0.0.1:8600"
DESCOBERTA_URL = "http://127.0.0.1:8502"
PORTA = 8700
UA = {"User-Agent": "Mozilla/5.0"}

# Funções que só a Carteira no Google sabe fazer (notificações e acionadores dela). Os registos fazem-se aqui, na folha,
# pela API (folha.py), quando a ligação está feita.
SO_NA_CARTEIRA = {
    "pedirPares", "atualizarHistorico", "gravarSincronizacao", "prepararPastaSync", "definirAlertasEmail", "ligarTelegram",
    "desligarTelegram", "testarTelegram",
}
import folha as FOLHA  # noqa: E402

# Mapeamento de tickers da folha (Google Finance) para o Yahoo: o mesmo da sincronização da Carteira
sys.path.insert(0, str(CARTEIRA_APP / "ferramentas"))
try:
    import sincronizar as S  # noqa: E402
except Exception:  # noqa: BLE001
    S = None

app = FastAPI(title="Centro de Investimento")


@app.middleware("http")
async def sem_cache(pedido, seguinte):
    """Os ficheiros da página revalidam-se sempre: uma versão nova do Centro aparece logo, sem limpar a cache do browser."""
    resposta = await seguinte(pedido)
    if pedido.url.path.startswith(("/web/", "/carteira/")):
        resposta.headers["Cache-Control"] = "no-cache"
    return resposta
_cache: dict[str, tuple[float, object]] = {}


def em_cache(chave: str, segundos: int, f):
    agora = time.time()
    if chave in _cache and agora - _cache[chave][0] < segundos:
        return _cache[chave][1]
    v = f()
    _cache[chave] = (agora, v)
    return v


def http_json(url: str, timeout: int = 20, dados: bytes | None = None, metodo: str | None = None) -> dict:
    h = dict(UA)
    if dados is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=dados, headers=h, method=metodo)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ler_json(p: Path):
    if not p.exists():
        return None
    for _ in range(3):  # o Google Drive pode estar a meio de escrever
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            time.sleep(0.4)
    return None


def limpo(v):
    """NaN e infinitos passam a null (o JSON não os aceita)."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, dict):
        return {k: limpo(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [limpo(x) for x in v]
    return v


def ticker_yahoo(t: str) -> str:
    if S:
        try:
            return S.ticker_yahoo(t)
        except Exception:  # noqa: BLE001
            pass
    return t.split(":", 1)[-1]


# ---------------------------------------------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def inicio():
    return FileResponse(AQUI / "web" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/carteira/", include_in_schema=False)
def carteira_pagina():
    """A interface da Carteira de Ativos tal como está no Google, com um google.script.run que fala com este servidor."""
    ler = lambda n: (CARTEIRA_UI / n).read_text(encoding="utf-8")  # noqa: E731
    ponte = (AQUI / "web" / "ponte_carteira.js").read_text(encoding="utf-8")
    html = ler("Index.html").replace(
        '<meta charset="utf-8">', '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">')
    html = re.sub(r"<\?!= incluir\('(\w+)'\); \?>",
                  lambda m: (f"<script>{ponte}</script>" if m.group(1) == "Calculos" else "") + ler(m.group(1) + ".html"), html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/carteira/calculos.js", include_in_schema=False)
def carteira_calculos():
    """O motor de cálculo da Carteira (o mesmo ficheiro, testado contra a folha), para o Painel usar os mesmos números."""
    src = (CARTEIRA_UI / "Calculos.html").read_text(encoding="utf-8")
    js = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", src))
    return HTMLResponse(js + "\nwindow.Calc = Calc;", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------------------------------------------
# Carteira: dados e funções do servidor do Google, aqui feitas no PC
# ---------------------------------------------------------------------------------------------------------------
@app.get("/api/folha/estado")
def folha_estado():
    out = {"chave": FOLHA.disponivel(), "email": FOLHA.email_conta(), "ligada": False, "erro": None}
    if out["chave"]:
        try:
            FOLHA.ler(["'Cotações'!A1"])
            out["ligada"] = True
        except Exception as e:  # noqa: BLE001
            out["erro"] = str(e)[:400]
    return out


@app.get("/api/carteira/dados")
def carteira_dados():
    j = ler_json(EXPORT_CARTEIRA) or {}
    d = None
    if FOLHA.disponivel() and not os.environ.get("CENTRO_DADOS_TESTE"):
        try:  # ao vivo, da folha; o que só o Apps Script sabe (notificações, pedidos) vem da última exportação
            d = FOLHA.ler_dados()
            extra = j.get("dados") or {}
            d["config"]["notificacoes"] = (extra.get("config") or {}).get("notificacoes") or {}
            d["config"]["emailAlertas"] = d["config"]["notificacoes"].get("email", False)
            d["benchmarks"] = extra.get("benchmarks") or benchmarks_carteira()
            d["urlFolha"] = f"https://docs.google.com/spreadsheets/d/{FOLHA.ID_FOLHA}/edit"
            d["pedidos"] = extra.get("pedidos") or []
            d["sync"] = {"estado": "pc", "quando": d["agora"]}
            d["exportado"] = "folha ao vivo, " + d["agora"]
            d["aoVivo"] = True
        except Exception as e:  # noqa: BLE001
            d = None
            j.setdefault("_erroFolha", str(e)[:300])
    if d is None:
        if not j.get("dados"):
            raise HTTPException(503, "Ainda não há dados da Carteira no PC. Liga a folha Google (Ferramentas do Centro) ou abre a "
                                     "Carteira no Google: ela grava «carteira_dados.json» na pasta «Carteira - sincronização».")
        d = j["dados"]
        d["sync"] = {"estado": "pc", "quando": j.get("exportado")}
        d["exportado"] = j.get("exportado") + (" (cópia; a folha não respondeu)" if j.get("_erroFolha") else "")
        d["aoVivo"] = False
        e = estado_fila(j.get("exportado"))
        aplicar_fila(d, e["pendentes"] + e["fora_da_copia"])
    # Técnica e decisão mais recentes do PC (a cópia da Carteira pode ter até 10 minutos)
    tec = ler_json(PASTA_DRIVE / "tecnica_carteira.json")
    if tec:
        d["tecnica"] = tec
    # Ticker da folha -> tickers possíveis na Decisão (Yahoo e os nomes dos relatórios, ex.: LON:REL -> REL.L e RELX)
    inverso = {v: k for k, v in (getattr(S, "ALIAS", {}) or {}).items()}
    d["mapaDecisao"] = {c["ticker"]: list(dict.fromkeys(x for x in (c["ticker"], inverso.get(c["ticker"]), ticker_yahoo(c["ticker"]), c["ticker"].split(":")[-1]) if x))
                        for c in d.get("cotacoes", [])}
    _relatorios_frescos(d)
    return d


def _relatorios_frescos(d: dict) -> None:
    """Preço-alvo, recomendação, tese e fosso das posições e da watchlist (coluna «Relatório» da Carteira) passam a vir do
    relatório mais recente no disco, como no resto do Centro. A cópia da Carteira traz a última sincronização, que pode estar
    atrasada ou mal lida (CTAS tinha o preço-alvo de um analista). Campo sem valor no relatório = lacuna, nunca o antigo."""
    try:
        frescos = em_cache("resumo-carteira", 120, decisao_resumo)["empresas"]
    except Exception:  # noqa: BLE001  (sem o índice da Decisão fica a cópia da Carteira)
        return
    rels = d.setdefault("relatorios", {}).setdefault("empresas", {})
    por_base = {k.split(":")[-1].split(".")[0]: k for k in rels}
    for t, x in frescos.items():
        r = x.get("relatorio") or {}
        if not str(r.get("origem", "")).startswith("disco"):
            continue
        chave = t if t in rels else por_base.get(t.split(":")[-1].split(".")[0], t)
        antigo = rels.get(chave) or {}
        novo = dict(antigo, nome=antigo.get("nome") or x.get("nome"), lista=antigo.get("lista") or x.get("lista"))
        for k in ("recomendacao", "precoAlvo", "moedaAlvo", "tese", "fosso"):
            novo[k] = r.get(k)
        if antigo.get("precoAlvo") != r.get("precoAlvo"):
            novo["excertoAlvo"] = None  # o excerto antigo já não é deste preço-alvo
        novo["relatorio"], novo["data"] = r.get("relatorio") or r.get("ficheiro"), r.get("data")
        novo["origem"] = f"relatório mais recente no disco ({r.get('data') or 'sem data'}), lido pelo Centro"
        rels[chave] = novo


def _velas(simbolo: str) -> dict:
    s = simbolo.strip().upper()
    j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range=2y&interval=1d")
    res = j["chart"]["result"][0]
    q, m = res["indicators"]["quote"][0], res.get("meta", {})
    div = 100 if m.get("currency") == "GBp" else 1
    out = {"simbolo": s, "moeda": "GBP" if m.get("currency") == "GBp" else m.get("currency"), "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for i, t in enumerate(res.get("timestamp") or []):
        if q["close"][i] is None or q["open"][i] is None:
            continue
        out["t"].append(datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"))
        for k, kk in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
            out[k].append(q[kk][i] / div)
        out["v"].append(q["volume"][i] or 0)
    return out


def _cotacao_rapida(simbolo: str) -> dict:
    s = simbolo.strip().upper()
    j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range=1y&interval=1wk")
    res = (j.get("chart") or {}).get("result")
    if not res:
        raise ValueError(f"Não encontrei {s} no Yahoo Finance.")
    res = res[0]
    m, cl = res.get("meta", {}), res["indicators"]["quote"][0].get("close") or []
    serie = [[datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), round(c, 2)] for t, c in zip(res.get("timestamp") or [], cl) if c is not None]
    p = m.get("regularMarketPrice")
    return {"simbolo": m.get("symbol", s), "nome": m.get("longName") or m.get("shortName") or s, "moeda": m.get("currency"),
            "bolsa": m.get("fullExchangeName") or m.get("exchangeName"), "tipo": m.get("instrumentType"), "preco": p,
            "max52": m.get("fiftyTwoWeekHigh"), "min52": m.get("fiftyTwoWeekLow"),
            "variacaoAno": (p / serie[0][1] - 1) if serie and p else None, "serie": serie}


def _pesquisar(q: str) -> list:
    q = (q or "").strip()
    if not q:
        return []
    j = http_json("https://query2.finance.yahoo.com/v1/finance/search?lang=pt-PT&quotesCount=10&newsCount=0&q=" + urllib.parse.quote(q))
    return [{"simbolo": x["symbol"], "nome": x.get("longname") or x.get("shortname") or x["symbol"], "bolsa": x.get("exchDisp") or x.get("exchange", ""),
             "tipo": "ETF" if x.get("quoteType") == "ETF" else "Ação", "setor": x.get("sectorDisp") or x.get("sector", "")}
            for x in j.get("quotes", []) if x.get("symbol") and x.get("quoteType") in ("EQUITY", "ETF")]


def _cotacao_dia(simbolo: str) -> dict:
    """Último preço e variação face ao fecho da sessão anterior (com range=1d, chartPreviousClose é esse fecho)."""
    s = simbolo.strip().upper()
    try:
        m = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range=1d&interval=1d")["chart"]["result"][0]["meta"]
        p, ant = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
        return {"simbolo": s, "preco": p, "moeda": m.get("currency"), "variacao": (p / ant - 1) if p and ant else None}
    except Exception:  # noqa: BLE001 — sem preço, a sugestão aparece na mesma
        return {"simbolo": s, "preco": None, "moeda": None, "variacao": None}


def _cotacoes_dia(simbolos: list) -> list:
    from concurrent.futures import ThreadPoolExecutor
    lista = [str(x) for x in (simbolos or [])][:8]
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(lambda s: em_cache("d:" + s.upper(), 300, lambda: _cotacao_dia(s)), lista))


def _noticias(simbolo: str, nome: str = "") -> list:
    """Como na Carteira; os títulos ficam no original quando a tradução não está disponível (aqui não há LanguageApp)."""
    s = simbolo.strip().upper()
    q = s.split(".")[0]
    try:  # traduções já feitas pela Decisão de Investimento
        noticias = http_json(f"{DECISAO}/api/empresa/{urllib.parse.quote(s)}/noticias", 30)
        lista = noticias.get("noticias") if isinstance(noticias, dict) else noticias
        if lista:
            return [{"titulo": n.get("titulo") or n.get("title"), "original": n.get("original") or n.get("titulo"), "fonte": n.get("fonte", ""),
                     "url": n.get("url") or n.get("link"), "quando": n.get("quando") or n.get("data"), "tickers": n.get("tickers", []),
                     "relevante": n.get("relevante", True), "imagem": n.get("imagem")} for n in lista]
    except Exception:  # noqa: BLE001
        pass
    j = http_json(f"https://query2.finance.yahoo.com/v1/finance/search?lang=en-US&quotesCount=0&newsCount=15&q={urllib.parse.quote(q)}")
    out = []
    for n in j.get("news", []):
        if not (n.get("title") and n.get("link")):
            continue
        rel = any(str(t).upper().split(".")[0] == q for t in n.get("relatedTickers") or []) or re.search(rf"\b{re.escape(q)}\b", n["title"]) is not None
        out.append({"titulo": n["title"], "original": n["title"], "fonte": n.get("publisher", ""), "url": n["link"],
                    "quando": datetime.utcfromtimestamp(n["providerPublishTime"]).isoformat() + "Z" if n.get("providerPublishTime") else None,
                    "tickers": n.get("relatedTickers") or [], "relevante": rel, "imagem": None})
    return sorted(out, key=lambda x: x["quando"] or "", reverse=True)


def _fora_de_horas(simbolos: list) -> dict:
    out = {}
    agora = int(time.time())
    for s in {str(x).upper() for x in simbolos or []}:
        v = {"estado": "fechado"}
        try:
            res = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range=1d&interval=5m&includePrePost=true", 10)["chart"]["result"][0]
            m, per = res.get("meta", {}), res.get("meta", {}).get("currentTradingPeriod", {})
            div = 100 if m.get("currency") == "GBp" else 1
            ts, cl = res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []
            ultimo = next(((ts[i], cl[i] / div) for i in range(len(ts) - 1, -1, -1) if cl[i] is not None), None)
            fecho, ant = (m.get("regularMarketPrice") or 0) / div, (m.get("chartPreviousClose") or m.get("previousClose") or 0) / div
            pre, pos, reg = per.get("pre"), per.get("post"), per.get("regular")
            if pre and pre["start"] <= agora < pre["end"] and ultimo and ultimo[0] >= pre["start"]:
                v = {"estado": "pre", "preco": ultimo[1], "variacao": ultimo[1] / ant - 1 if ant else None, "hora": ultimo[0]}
            elif pos and pos["start"] <= agora < pos["end"] and ultimo and ultimo[0] >= pos["start"]:
                v = {"estado": "pos", "preco": ultimo[1], "variacao": ultimo[1] / fecho - 1 if fecho else None, "hora": ultimo[0]}
            elif reg and reg["start"] <= agora < reg["end"]:
                v = {"estado": "regular"}
            v["moeda"] = "GBP" if m.get("currency") == "GBp" else m.get("currency")
        except Exception:  # noqa: BLE001
            v = {"estado": "erro"}
        out[s] = v
    return out


def _cambios_bce(pedidos) -> dict:
    """Como na Carteira: {'USD': ['2026-09-30']} -> {'USD': {'2026-09-30': 0.85}} (taxas de referência do BCE, pela frankfurter.app).
    Fim de semana ou feriado: vale a última taxa publicada antes dessa data."""
    out = {}
    for moeda, datas in (pedidos or {}).items():
        datas = sorted(datas or [])
        if not datas:
            continue
        ini = (datetime.strptime(datas[0], "%Y-%m-%d").toordinal() - 7)
        ini = datetime.fromordinal(ini).strftime("%Y-%m-%d")
        try:
            taxas = http_json(f"https://api.frankfurter.app/{ini}..{datas[-1]}?from={moeda}&to=EUR", 20).get("rates", {})
        except Exception:  # noqa: BLE001
            continue
        dias = sorted(taxas)
        out[moeda] = {}
        for d in datas:
            ult = max((x for x in dias if x <= d), default=None)
            if ult:
                out[moeda][d] = taxas[ult]["EUR"]
    return out


_triagens_pc: dict[str, dict] = {}


def _obter_triagem(ticker: str) -> dict:
    t = ticker.strip().upper()
    r = {"ticker": t, "triagem": None, "pendente": False, "idadeHoras": None, "erro": None, "pastaOk": PASTA_DRIVE.exists()}
    loc = _triagens_pc.get(t)
    if loc and loc.get("pendente"):
        r.update(pendente=True, pedidoEm=loc["em"])
        return r
    fonte = loc if loc and loc.get("gerado") else (ler_json(TRIAGENS) or {}).get(t)
    if fonte:
        idade = (datetime.now() - datetime.strptime(fonte["gerado"], "%Y-%m-%d %H:%M")).total_seconds() / 3600
        r.update(idadeHoras=round(idade, 1), gerado=fonte["gerado"])
        if fonte.get("ok"):
            r["triagem"] = fonte["dados"]
        else:
            r["erro"] = fonte.get("erro")
    return r


def _pedir_triagem(ticker: str, forcar: bool = False) -> dict:
    """No PC a triagem faz-se logo, pela Decisão de Investimento (sem esperar pelo vigilante da pasta do Drive)."""
    t = ticker.strip().upper()
    atual = _obter_triagem(t)
    if atual["pendente"] or (atual["triagem"] and not forcar and (atual["idadeHoras"] or 99) < 24):
        return atual
    if not S:
        raise ValueError("Não consegui carregar o motor de triagem (ferramentas\\sincronizar.py da Carteira).")
    _triagens_pc[t] = {"pendente": True, "em": datetime.now().strftime("%H:%M")}

    def correr():
        try:
            _triagens_pc[t] = {"ok": True, "dados": S.triagem(t), "gerado": datetime.now().strftime("%Y-%m-%d %H:%M")}
        except Exception as e:  # noqa: BLE001
            _triagens_pc[t] = {"ok": False, "erro": str(e), "gerado": datetime.now().strftime("%Y-%m-%d %H:%M")}

    threading.Thread(target=correr, daemon=True).start()
    return _obter_triagem(t)


FUNCOES = {
    "velas": lambda s: em_cache("v:" + s, 600, lambda: _velas(s)),
    "cotacaoRapida": lambda s: em_cache("c:" + s, 900, lambda: _cotacao_rapida(s)),
    "pesquisarTicker": lambda q: em_cache("p:" + q.lower(), 3600, lambda: _pesquisar(q)),
    "cotacoesDia": _cotacoes_dia,  # só do Centro (sugestões da Pesquisa)
    "noticiasHoje": lambda s, nome="": em_cache("n:" + s, 1200, lambda: _noticias(s, nome)),
    "foraDeHoras": _fora_de_horas,
    "cambiosBce": _cambios_bce,
    "obterTriagem": _obter_triagem,
    "pedirTriagem": _pedir_triagem,
    # O registo oficial dos alertas é o da Carteira no Google; aqui só se mostram, com a hora
    "registarAlertas": lambda lista: [dict(a, data=datetime.now().strftime("%d/%m %H:%M")) for a in (lista or [])][:10],
    "obterDados": lambda: carteira_dados(),
}


# ---------------------------------------------------------------------------------------------------------------
# Registos feitos no Centro: fila na pasta partilhada do Drive. A Carteira no Google grava-os na folha de minuto a
# minuto (processarOrdensCentro, autorizado pelo Rui a 10/10/2026) e responde em ordens_resultado.json.
# Até a cópia da Carteira os trazer, o Centro mostra-os já aplicados («a caminho da folha»).
# ---------------------------------------------------------------------------------------------------------------
FILA = PASTA_DRIVE / "ordens_centro.json"
RESULTADOS_FILA = PASTA_DRIVE / "ordens_resultado.json"
_tranca_fila = threading.Lock()


def _hora(s: str | None) -> datetime | None:
    for f in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(str(s), f)
        except ValueError:
            pass
    return None


def enfileirar(fn: str, args: list):
    """Deixa a ordem na fila e devolve o que a função da Carteira devolveria (a linha fica «p<id>» até ser gravada)."""
    cancelar = fn in ("apagarMovimento", "apagarDeposito") and str(args[0]).startswith("p")
    with _tranca_fila:
        fila = ler_json(FILA) or {"ordens": []}
        res = ler_json(RESULTADOS_FILA) or {}
        if cancelar:
            alvo = str(args[0])[1:]
            if alvo in res:
                raise ValueError("Esse registo ainda está a ser gravado na folha. Tenta outra vez daqui a um minuto.")
            fila["ordens"] = [o for o in fila["ordens"] if o["id"] != alvo]  # ainda não saiu: basta tirá-lo da fila
        else:
            fila["ordens"].append({"id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "fn": fn, "args": args,
                                   "em": datetime.now().strftime("%d/%m/%Y %H:%M:%S")})
        # As ordens gravadas há mais de um dia saem da fila
        limite = datetime.now().timestamp() - 86400
        fila["ordens"] = [o for o in fila["ordens"]
                          if not (o["id"] in res and (_hora(res[o["id"]].get("quando")) or datetime.now()).timestamp() < limite)]
        with open(FILA, "w", encoding="utf-8") as f:  # escrita no lugar: o Google Drive sincroniza o mesmo ficheiro
            json.dump(fila, f, ensure_ascii=False)
    if cancelar:
        return True
    ident = fila["ordens"][-1]["id"]
    if fn in ("gravarMovimento", "gravarDeposito"):
        return {"linha": (args[0] or {}).get("linha") or "p" + ident}
    if fn == "gravarImportacao":
        lote = args[0] or {}
        return {"gravados": len(lote.get("movimentos") or []) + len(lote.get("depositos") or [])}
    return True


def estado_fila(exportado: str | None = None) -> dict:
    """Ordens ainda por gravar, gravadas mas ainda fora da cópia, e erros."""
    fila, res = (ler_json(FILA) or {"ordens": []})["ordens"], ler_json(RESULTADOS_FILA) or {}
    exp = _hora(exportado)
    pendentes, fora_da_copia, erros = [], [], []
    for o in fila:
        r = res.get(o["id"])
        if not r:
            pendentes.append(o)
        elif not r.get("ok"):
            erros.append({**o, "erro": r.get("erro"), "quando": r.get("quando")})
        elif not exp or (_hora(r.get("quando")) or datetime.now()).replace(second=0, microsecond=0) > exp:
            # Gravada, mas a cópia da Carteira ainda é anterior. A hora da cópia só tem minutos, e a Carteira refaz a cópia
            # logo depois de gravar: no mesmo minuto conta como já incluída.
            fora_da_copia.append(o)
    return {"pendentes": pendentes, "fora_da_copia": fora_da_copia, "erros": erros[-5:]}


def aplicar_fila(d: dict, ordens: list) -> None:
    """Mostra na cópia os registos ainda a caminho da folha, como a Carteira os vai gravar."""
    for o in ordens:
        fn, a, pid = o["fn"], o.get("args") or [None], "p" + o["id"]
        if fn in ("gravarMovimento", "gravarDeposito"):
            lista = d.setdefault("movimentos" if fn == "gravarMovimento" else "depositos", [])
            novo = dict(a[0] or {})
            novo["linha"] = novo.get("linha") or pid
            lista[:] = [x for x in lista if str(x.get("linha")) != str(novo["linha"])] + [novo]
        elif fn in ("apagarMovimento", "apagarDeposito"):
            lista = d.get("movimentos" if fn == "apagarMovimento" else "depositos", [])
            lista[:] = [x for x in lista if str(x.get("linha")) != str(a[0])]
        elif fn == "gravarImportacao":
            for i, m in enumerate((a[0] or {}).get("movimentos") or []):
                d.setdefault("movimentos", []).append(dict(m, linha=f"{pid}-{i}"))
            for i, x in enumerate((a[0] or {}).get("depositos") or []):
                d.setdefault("depositos", []).append(dict(x, linha=f"{pid}-d{i}"))
        elif fn == "gravarAlertas":
            d["alertasManuais"] = [{"ticker": x.get("ticker"), "condicao": x.get("condicao"), "preco": x.get("preco"), "nota": x.get("nota", "")} for x in a[0] or []]
        elif fn == "gravarClassificacao":
            d["classificacao"] = {c["ticker"]: {k: c.get(k, "") for k in ("setor", "pais", "paisFonte", "codPais", "moeda", "yahoo")} for c in a[0] or [] if c.get("ticker")}
        elif fn == "gravarConfig":
            d.setdefault("config", {}).update(a[0] or {})
        elif fn == "gravarPpr":
            d["pprValor"] = a[0]
        elif fn == "adicionarTicker" and a[0] and not any(c.get("ticker") == a[0] for c in d.get("cotacoes", [])):
            d.setdefault("cotacoes", []).append({"ticker": a[0], "tipo": "ETF" if (a[1:] or [None])[0] == "ETF" else "Ação", "nome": a[0], "preco": "", "precoEur": ""})


@app.get("/api/carteira/fila")
def carteira_fila():
    j = ler_json(EXPORT_CARTEIRA) or {}
    e = estado_fila(j.get("exportado"))
    return {"a_caminho": len(e["pendentes"]) + len(e["fora_da_copia"]), "por_gravar": len(e["pendentes"]),
            "erros": [{"fn": x["fn"], "erro": x["erro"], "quando": x["quando"]} for x in e["erros"]], "copia": j.get("exportado")}


def benchmarks_carteira() -> list:
    """Os índices de comparação, lidos do próprio Servidor.js da Carteira (um só sítio)."""
    try:
        src = (CARTEIRA_UI / "Servidor.js").read_text(encoding="utf-8")
        bloco = re.search(r"var BENCHMARKS = \{([\s\S]*?)\n\};", src).group(1)
        return [{"chave": k, "nome": n} for k, n in re.findall(r"'([^']+)':\s*\{[^}]*nome:\s*'([^']+)'", bloco)]
    except Exception:  # noqa: BLE001
        return []


def _pedir_decisao(acao: str, ticker: str) -> list:
    """Os pedidos da Carteira à Decisão (watchlist, carteira, retirar, triagem) fazem-se aqui na hora, sem a pasta do Drive."""
    t = ticker.strip().upper()
    destino = {"watchlist": "observacao", "carteira": "carteira", "retirar": "outros"}.get(acao)
    agora = datetime.now().strftime("%d/%m %H:%M")
    if acao == "triagem":
        _pedir_triagem(t, True)
        res = "Triagem pedida à Decisão de Investimento."
    elif destino:
        mover_lista(destino, {"ticker": t})
        res = {"observacao": "Posto em observação.", "carteira": "Passou para a carteira.", "outros": "Retirado das listas."}[destino]
    else:
        raise ValueError("Pedido inválido.")
    return [{"id": agora, "em": agora, "acao": acao, "ticker": t, "estado": "feito", "resultado": res, "tratado": agora}]


@app.post("/api/carteira/run/{nome}")
def carteira_run(nome: str, args: list = Body(default=[])):
    if nome in SO_NA_CARTEIRA:
        raise HTTPException(409, "Isto (notificações, histórico semanal e concorrentes da triagem) faz-se na Carteira no Google; "
                                 "o resto já se faz aqui.")
    if nome in FOLHA.ESCRITAS:
        if not FOLHA.disponivel():
            try:  # pela fila da pasta do Drive: a Carteira no Google grava-a no minuto seguinte
                return JSONResponse(limpo(enfileirar(nome, args)))
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        try:
            return JSONResponse(limpo(FOLHA.ESCRITAS[nome](*args)))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, "A folha Google não aceitou a escrita: " + str(e)[:300]) from e
    if nome == "pedirDecisao":
        return JSONResponse(_pedir_decisao(*args))
    if nome == "cancelarPedido":
        return JSONResponse([])
    f = FUNCOES.get(nome)
    if not f:
        raise HTTPException(501, f"A função «{nome}» ainda não existe no Centro de Investimento.")
    try:
        return JSONResponse(limpo(f(*args)))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)) from e


# ---------------------------------------------------------------------------------------------------------------
# Decisão de Investimento, Descoberta e estado geral
# ---------------------------------------------------------------------------------------------------------------
def _decisao_viva() -> bool:
    try:
        urllib.request.urlopen(DECISAO + "/api/regras", timeout=3).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def _descoberta_viva() -> bool:
    try:
        urllib.request.urlopen(DESCOBERTA_URL, timeout=3).read()
        return True
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/estado")
def estado():
    exp = ler_json(EXPORT_CARTEIRA)
    sync = ler_json(SYNC_DECISAO) or {}
    return {
        "agora": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "decisao": {"ativa": _decisao_viva(), "url": "http://localhost:8600"},
        "descoberta": {"ativa": _descoberta_viva(), "url": "http://localhost:8502"},
        "carteira": {"exportado": exp.get("exportado") if exp else None, "pasta": PASTA_DRIVE.exists()},
        "sincronizacao": sync.get("gerado"),
    }


@app.get("/api/decisao/resumo")
def decisao_resumo():
    """Análises da Decisão (todas as empresas da carteira e da observação) e os relatórios do Cowork, numa só leitura."""
    sync = ler_json(SYNC_DECISAO) or {}
    dec = sync.get("decisao") or {}
    rel = (sync.get("relatorios") or {}).get("empresas") or {}
    out = {}
    for t, a in (dec.get("analises") or {}).items():
        d = a.get("decisao") or {}
        cal = (a.get("calendario") or {}).get("proximo") or {}
        cot = a.get("cotacao") or {}
        valor = ((a.get("resumo") or {}).get("valor") or {})
        out[t] = {
            "ticker": t, "nome": a.get("nome"), "lista": a.get("lista"), "moeda": a.get("moeda"), "preco": a.get("preco"),
            "variacao": cot.get("variacao"), "max52": cot.get("max_52s"), "min52": cot.get("min_52s"),
            "sinal": d.get("nivel"), "sinalRotulo": d.get("rotulo"), "pontos": d.get("pontos"),
            "pilares": [{"pilar": p.get("pilar"), "leitura": p.get("leitura"), "pontos": p.get("pontos")} for p in d.get("pilares") or []],
            "valorMediana": valor.get("mediana"), "proximosResultados": cal.get("data"), "diasResultados": cal.get("dias"),
            "momentoResultados": cal.get("momento"), "alertas": a.get("alertas") or [],
            "suportes": [s.get("preco") for s in ((a.get("niveis") or {}).get("suportes") or [])][:3],
        }
    base = {k.split(".")[0]: k for k in out}  # os relatórios usam ABB e BONEX; a Decisão usa ABB.ST e BONEX.ST
    for t, r in rel.items():
        if r.get("lista") not in ("carteira", "observacao") and t not in out and t not in base:
            continue  # análises antigas: ficam na Wiki da Decisão, não no Painel
        chave = t if t in out else base.get(t, t)
        alvo = out.setdefault(chave, {"ticker": chave, "nome": r.get("nome"), "lista": r.get("lista")})
        alvo["relatorio"] = {k: r.get(k) for k in ("recomendacao", "precoAlvo", "moedaAlvo", "tese", "fosso", "relatorio", "ficheiro", "data", "periodo")}
        alvo["relatorio"]["formato"] = {"estado": "desconhecido"}
    # O relatório mais recente vem do disco (índice da Decisão, refeito a cada leitura), não da sincronização,
    # que pode ter horas: um relatório gravado depois dela passaria despercebido.
    try:
        for t, alvo in out.items():
            rels = relatorios_ordenados(t)
            if not rels:
                continue
            res = relatorio_resumo(rels[0])
            res["origem"], res["herdado"] = "disco (índice da Decisão)", {}
            # Campo que o relatório mais recente não traz: recua ao anterior que o tenha, marcado e datado (nunca se inventa)
            if res["formato"].get("estado") != "invalido":
                for anterior in rels[1:]:
                    ant = relatorio_resumo(anterior)
                    for k in ("recomendacao", "tese", "precoAlvo", "fosso"):
                        if res.get(k) in (None, "") and ant.get(k) not in (None, ""):
                            res[k] = ant[k]
                            if k == "precoAlvo":
                                res["moedaAlvo"] = ant.get("moedaAlvo")
                            res["herdado"][k] = {"data": ant.get("data"), "ficheiro": ant.get("ficheiro")}
            alvo["relatorio"] = res
    except Exception:  # noqa: BLE001  (Decisão desligada: fica a sincronização, marcada)
        for alvo in out.values():
            if alvo.get("relatorio"):
                alvo["relatorio"]["origem"] = "sincronização de " + str(sync.get("gerado"))
    return {"gerado": dec.get("gerado") or sync.get("gerado"), "empresas": limpo(out), "listas": dec.get("listas")}


@app.get("/api/descoberta")
def descoberta():
    """Resultado mais recente de cada pesquisa da Descoberta (automáticas por região e a última manual)."""
    def ler():
        import pandas as pd
        ficheiros = []
        auto = DESCOBERTA / "automaticas"
        if auto.exists():
            for reg in auto.iterdir():
                pk = sorted(reg.glob("*.pkl"))
                if pk:
                    ficheiros.append((reg.name, pk[-1]))
        # Todas as pesquisas manuais (as do Centro e as da Descoberta): uma pesquisa nova não esconde as anteriores;
        # de cada empresa fica a análise mais recente
        for f in sorted(DESCOBERTA.glob("descoberta_*.pkl")):
            ficheiros.append(("pesquisa de " + datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m %H:%M"), f))
        nomes_pesq = {}
        try:
            nomes_pesq = {p["id"]: p["nome"] for p in _desc_mod("automatico").listar()}
        except Exception:  # noqa: BLE001
            pass
        linhas, fontes = {}, []
        for origem, f in ficheiros:
            df = pd.read_pickle(f)
            minha = origem in nomes_pesq  # resultado de uma pesquisa guardada = passou os critérios do Rui
            origem = nomes_pesq.get(origem, origem)
            fontes.append({"origem": origem, "minha": minha, "ficheiro": f.name, "data": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m/%Y %H:%M"), "n": len(df)})
            for r in df.to_dict("records"):
                t = r.get("Ticker")
                if t and (t not in linhas or linhas[t]["_data"] < f.stat().st_mtime):
                    r["_origem"], r["_data"], r["_minha"] = origem, f.stat().st_mtime, minha or (t in linhas and linhas[t].get("_minha"))
                    linhas[t] = r
        empresas = juntar_cotacoes(list(linhas.values()))
        # as que analisaste à mão (fora das pesquisas guardadas): à parte, para não se juntarem a outra cotação da mesma empresa
        manuais = _ler_analisadas()
        for r in empresas:
            if r["Ticker"] in manuais:
                r["_manual"] = manuais.pop(r["Ticker"]).get("gerado") or True
        for t, a in manuais.items():
            empresas.append(dict(a.get("linha") or {}, Ticker=t, _origem="analisada por ti a " + str(a.get("gerado", "")),
                                 _minha=False, _manual=a.get("gerado") or True, _outras=[]))
        return {"fontes": fontes, "empresas": limpo(empresas)}
    return JSONResponse(em_cache("descoberta", 300, ler))


# Bolsa da sede, para escolher a cotação principal de uma empresa cotada em várias (IAG: IAG.MC e não INR.DE)
SUFIXO_PAIS = {"United States": "", "Canada": ".TO", "United Kingdom": ".L", "Germany": ".DE", "France": ".PA", "Netherlands": ".AS",
               "Spain": ".MC", "Italy": ".MI", "Switzerland": ".SW", "Sweden": ".ST", "Denmark": ".CO", "Norway": ".OL",
               "Finland": ".HE", "Belgium": ".BR", "Portugal": ".LS", "Austria": ".VI", "Ireland": ".IR"}


def juntar_cotacoes(linhas: list) -> list:
    """Uma linha por empresa: as outras cotações ficam em «_outras». Fica a da bolsa da sede; sem ela, a análise mais recente."""
    grupos: dict[str, list] = {}
    for r in linhas:
        chave = re.sub(r"[^a-z0-9]", "", str(r.get("Empresa") or r["Ticker"]).lower())
        grupos.setdefault(chave, []).append(r)
    out = []
    for g in grupos.values():
        def sede(r):
            suf = SUFIXO_PAIS.get(r.get("País"))
            t = str(r["Ticker"])
            return suf is not None and ((suf == "" and "." not in t) or (suf and t.endswith(suf)))
        g.sort(key=lambda r: (not sede(r), -r["_data"]))
        principal = dict(g[0])
        principal["_outras"] = [r["Ticker"] for r in g[1:]]
        principal["_minha"] = any(r.get("_minha") for r in g)
        out.append(principal)
    return out


@app.post("/api/listas/{destino}")
def mover_lista(destino: str, corpo: dict = Body(...)):
    """Passa pela Decisão de Investimento (é ela que guarda as listas e move a pasta da empresa)."""
    if destino not in ("observacao", "carteira", "outros"):
        raise HTTPException(400, "Destino inválido.")
    try:
        r = http_json(f"{DECISAO}/api/listas/{destino}", 120, json.dumps(corpo).encode("utf-8"), "POST")
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, json.loads(e.read().decode("utf-8")).get("detail", str(e))) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "A Decisão de Investimento (localhost:8600) não está a responder.") from e
    return r


@app.post("/api/decisao/por_decidir")
def enviar_por_decidir(corpo: dict = Body(...)):
    """Empresas escolhidas na Pesquisa: a Análise Fundamental analisa-as sem as pôr em nenhuma lista («por decidir»)."""
    try:
        return http_json(f"{DECISAO}/api/por_decidir", 60, json.dumps(corpo).encode("utf-8"), "POST")
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, json.loads(e.read().decode("utf-8")).get("detail", str(e))) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "A Análise Fundamental (localhost:8600) não está a responder.") from e


@app.get("/api/decisao/por_decidir")
def ler_por_decidir():
    try:
        return http_json(f"{DECISAO}/api/por_decidir", 20)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------------------------------------------
# Pedidos ao Cowork (ficheiros JSON numa pasta que o Cowork lê)
# ---------------------------------------------------------------------------------------------------------------
@app.get("/api/cowork/pedidos")
def pedidos_lista():
    PEDIDOS_COWORK.mkdir(exist_ok=True)
    out = []
    for f in sorted(PEDIDOS_COWORK.glob("*.json"), reverse=True):  # os «.json.tmp» a meio da escrita ficam de fora
        j = ler_json(f) or {}
        j["_ficheiro"] = f.name
        j["_abandonado"] = em_curso_abandonado(j)
        out.append(j)
    return out


ABANDONO_HORAS = 6  # «em_curso» há mais do que isto = sessão do Cowork interrompida; deixa de bloquear pedidos novos


def em_curso_abandonado(p: dict) -> bool:
    if p.get("estado") != "em_curso":
        return False
    try:  # hora de Lisboa, como o PC
        return (datetime.now() - datetime.strptime(str(p.get("em_curso_desde")), "%Y-%m-%d %H:%M")).total_seconds() > ABANDONO_HORAS * 3600
    except ValueError:
        return True  # sem hora de arranque legível não se sabe quando começou: trata-se como abandonado


@app.post("/api/cowork/pedidos/{ficheiro}/libertar")
def pedido_libertar(ficheiro: str):
    """O Rui liberta um pedido preso em «em_curso»: passa a «bloqueado» com o motivo (não há estados novos no formato)."""
    if not re.fullmatch(r"[\w.\-]+\.json", ficheiro):
        raise HTTPException(400, "Nome de ficheiro inválido.")
    f = PEDIDOS_COWORK / ficheiro
    p = ler_json(f)
    if not p:
        raise HTTPException(404, "Pedido não encontrado.")
    if p.get("estado") != "em_curso":
        raise HTTPException(409, "Só se libertam pedidos em curso.")
    p.update(estado="bloqueado", bloqueado_em=datetime.now().strftime("%Y-%m-%d %H:%M"),
             motivo=f"Libertado pelo Rui no Centro: a sessão do Cowork não terminou (em curso desde {p.get('em_curso_desde') or 'hora desconhecida'}).")
    gravar_atomico(f, p)
    return p


FORMATO_PEDIDO = 2          # muda sempre que um campo do pedido muda (ver COWORK_instrucoes.md)
DATA_FICHA = "2026-10-10"   # relatórios a partir desta data têm de trazer a Ficha v1 e as linhas «Filtros:» e «Cenários:»
ESTADOS = ("pendente", "em_curso", "feito", "bloqueado")
TITULOS_BASELINE = ("Resumo de Baseline", "Novo Resumo de Baseline")


def indice_relatorios() -> list:
    """Relatórios no disco, pelo índice da Decisão (lê as pastas a cada pedido; conhece os nomes antigos). Cache de 60 s."""
    return em_cache("indice", 60, lambda: [r for r in http_json(DECISAO + "/api/indice", 90).get("relatorios", [])
                                           if r.get("ticker") and not r.get("arquivado") and r.get("tipo") != "ferramenta"])


def ticker_relatorios(t: str) -> str:
    """Ticker usado nos nomes dos relatórios: LON:REL -> RELX, ABB.ST -> ABB."""
    inverso = {v: k for k, v in (getattr(S, "ALIAS", {}) or {}).items()}
    return inverso.get(t, t.split(":")[-1].split(".")[0]).upper()


def relatorios_de(t: str) -> list:
    tr = ticker_relatorios(t)
    return [r for r in indice_relatorios() if str(r["ticker"]).upper() in (tr, t.upper())]


def relatorios_ordenados(t: str) -> list:
    """Do mais recente para o mais antigo, pela regra do CLAUDE.md: subpasta de período mais recente
    (acompanhamento, depois análise) e só depois a iniciação."""
    rank = {"acompanhamento": 2, "trimestral": 1, "fundamental": 1, "iniciacao": 0}
    return sorted(relatorios_de(t), key=lambda r: (r.get("ordem_periodo") or 0, rank.get(r.get("tipo"), 0), r.get("data") or ""), reverse=True)


def ultimo_relatorio(t: str) -> dict | None:
    rel = relatorios_ordenados(t)
    return rel[0] if rel else None


TESE = {"quebrada": "quebrada", "enfraquecida": "enfraquece", "reforcada": "reforça-se", "intacta": "mantém-se"}


def relatorio_resumo(r: dict) -> dict:
    """O que o Painel mostra do relatório, lido do índice da Decisão (Ficha quando existe; texto nos antigos)."""
    def valor(x):
        return x.get("valor") if isinstance(x, dict) else x
    return {"recomendacao": valor(r.get("recomendacao")), "precoAlvo": valor(r.get("preco_alvo")),
            "moedaAlvo": (r.get("preco_alvo") or {}).get("moeda") if isinstance(r.get("preco_alvo"), dict) else None,
            "tese": TESE.get(valor(r.get("veredito")) or "", valor(r.get("veredito"))), "fosso": valor(r.get("fosso")),
            "relatorio": r.get("titulo"), "ficheiro": r.get("caminho"), "data": r.get("data"), "periodo": r.get("periodo"),
            "formato": formato_relatorio(r.get("caminho"))}


def baseline_de(t: str) -> dict | None:
    r = ultimo_relatorio(t)
    if not r:
        return None
    try:
        texto = (RELATORIOS / r["caminho"]).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        texto = ""
    # Título de secção (h1 a h4) com «resumo de baseline»: os relatórios novos usam os títulos fixos; os antigos têm variantes
    # («Resumo de baseline atualizado», «RESUMO DE BASELINE ACTUALIZADO»), que também servem de baseline.
    titulos = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip() for h in re.findall(r"<h[1-4][^>]*>([\s\S]*?)</h[1-4]>", texto, re.I)]
    achado = next((h for h in titulos if "resumo de baseline" in h.lower()), None)
    return {"ficheiro": r["caminho"], "data": r.get("data"), "periodo": r.get("periodo"), "tipo": r.get("tipo"),
            "tem_resumo_baseline": bool(achado), "titulo_resumo": achado,
            "titulo_fixo": bool(achado) and any(re.search(rf"(^|\d\s?){t}$", achado) for t in TITULOS_BASELINE)}


def formato_relatorio(caminho: str | None) -> dict:
    """«valido» (Ficha v1 e linhas «Filtros:»/«Cenários:» reconhecidas), «invalido» (a partir de 10/10/2026 sem isso) ou «antigo»."""
    r = next((x for x in indice_relatorios() if x.get("caminho") == caminho), None) if caminho else None
    if not r:
        return {"estado": "desconhecido"}
    ficha, base = r.get("ficha") or {}, r.get("baseline") or {}
    if (r.get("data") or "") < DATA_FICHA:
        return {"estado": "antigo"}
    erros = []
    if ficha.get("estado") != "ok":
        erros.append("Ficha de dados v1 " + ("ausente" if ficha.get("estado") == "ausente" else "não reconhecida"))
    if base.get("estado") != "ok":
        erros += base.get("erros") or ["linhas «Filtros:» e «Cenários:» ausentes ou fora do formato"]
    return {"estado": "invalido" if erros else "valido", "erros": erros, "lacunas": ficha.get("lacunas") or []}


def gravar_atomico(destino: Path, obj: dict) -> None:
    """Escreve num ficheiro temporário e só depois muda o nome: o Cowork nunca lê um pedido a meio. UTF-8 sem BOM."""
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.write_bytes(json.dumps(limpo(obj), ensure_ascii=False, indent=2).encode("utf-8"))
    os.replace(tmp, destino)


def linha_cotacao(ticker_carteira: str | None, t: str) -> dict:
    """Nome, bolsa e moeda da linha de cotação: a da Carteira se tiveres a ação; senão a do Yahoo."""
    if ticker_carteira:
        exp = (ler_json(EXPORT_CARTEIRA) or {}).get("dados") or {}
        c = next((x for x in exp.get("cotacoes", []) if x.get("ticker") == ticker_carteira), None)
        if c:
            return {"nome": c.get("nome"), "bolsa": c.get("bolsa"), "moeda": c.get("moeda"), "origem_cotacao": "Carteira (Google Finance)"}
    try:
        y = em_cache("c:" + ticker_yahoo(t), 900, lambda: _cotacao_rapida(ticker_yahoo(t)))
        return {"nome": y.get("nome"), "bolsa": y.get("bolsa"), "moeda": y.get("moeda"), "origem_cotacao": "Yahoo Finance"}
    except Exception:  # noqa: BLE001
        return {"nome": None, "bolsa": None, "moeda": None, "origem_cotacao": "lacuna"}


@app.post("/api/cowork/pedidos")
def pedido_criar(corpo: dict = Body(...)):
    t = str(corpo.get("ticker", "")).strip().upper()
    tipo = corpo.get("tipo")
    if not re.fullmatch(r"[A-Z0-9.:\-]{1,20}", t) or tipo not in ("iniciacao", "acompanhamento"):
        raise HTTPException(400, "Pedido inválido (ticker ou tipo).")
    PEDIDOS_COWORK.mkdir(exist_ok=True)
    # Pedidos incoerentes não chegam a ser criados
    if any(p.get("ticker") == t and p.get("tipo") == tipo and (p.get("estado") == "pendente" or (p.get("estado") == "em_curso" and not p.get("_abandonado")))
           for p in pedidos_lista()):
        raise HTTPException(409, f"Já há um pedido de {tipo} de {t} pendente ou em curso.")
    try:
        existentes = relatorios_de(t)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "Não consigo verificar os relatórios no disco: a Decisão de Investimento (8600) não responde.") from e
    base = baseline_de(t)
    if tipo == "iniciacao" and existentes:
        raise HTTPException(409, f"{t} já tem relatório no disco ({base['ficheiro']}). Pede um acompanhamento.")
    if tipo == "acompanhamento":
        if not base:
            raise HTTPException(409, f"{t} não tem relatório no disco: não há baseline. Pede uma iniciação.")
        if not base["tem_resumo_baseline"]:
            raise HTTPException(409, f"O baseline de {t} ({base['ficheiro']}) não tem a secção «Resumo de Baseline». "
                                     "Escolhe o baseline com o Rui antes de pedir.")
    posicao = corpo.get("posicao") or {"tem_posicao": False}
    agora = datetime.now()
    dados_app = corpo.get("dados_app")
    if dados_app:
        dados_app["aviso"] = "Só referência. Nunca é o preço de decisão: confirmar o preço e indicar a data."
    pedido = {
        "versao": FORMATO_PEDIDO, "estado": "pendente", "ticker": t, "ticker_relatorios": ticker_relatorios(t),
        **linha_cotacao(posicao.get("ticker_carteira"), t), "tipo": tipo, "pedido_em": agora.strftime("%Y-%m-%d %H:%M"),
        "nota": str(corpo.get("nota", ""))[:1000], "posicao": posicao, "baseline": base if tipo == "acompanhamento" else None,
        "dados_app": dados_app,
        # Opcional (formato 2): os cenários do motor da Descoberta, quando o pedido sai de «Analisar uma empresa».
        # Pressupostos genéricos: ponto de partida, nunca o DCF do relatório.
        "pressupostos_descoberta": corpo.get("pressupostos_descoberta") if isinstance(corpo.get("pressupostos_descoberta"), dict) else None,
        "instrucoes": ("Formato " + str(FORMATO_PEDIDO) + ": ver COWORK_instrucoes.md. Ao começar: estado «em_curso» e «em_curso_desde». "
                       "No fim: estado «feito», «feito_em» e «relatorio» (caminho real, relativo a Estudos Empresas-Relatorios). "
                       "Sem condições: estado «bloqueado», «bloqueado_em» e «motivo». Não mudar o nome deste ficheiro."),
    }
    nome = f"{agora:%Y-%m-%d_%H%M}_{t.replace(':', '-')}_{tipo}.json"
    gravar_atomico(PEDIDOS_COWORK / nome, pedido)
    return {"ficheiro": nome, **pedido}


# ---------------------------------------------------------------------------------------------------------------
# Pesquisa de compras: o motor da Descoberta de Ações a correr aqui (análise de uma empresa e pesquisas em lote)
# ---------------------------------------------------------------------------------------------------------------
DESC_APP = DESCOBERTA.parent
LOG_LOTE = AQUI / "pesquisa_lote.log"
_lote: dict = {"proc": None, "inicio": None, "args": None}
_analises: dict[str, dict] = {}


def _motor():
    if str(DESC_APP) not in sys.path:
        sys.path.insert(0, str(DESC_APP))
    import motor  # noqa: E402  (Descoberta de Ações)
    return motor


@app.post("/api/pesquisa/analisar/{ticker}")
def pesquisa_analisar(ticker: str, atualizar: bool = False, guardar: bool = False):
    """Análise completa de uma empresa pelo motor da Descoberta (DCF em três cenários, DCF inverso, moat, qualidade, alertas)."""
    t = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-^=]{1,20}", t):
        raise HTTPException(400, "Ticker inválido. Usa o formato do Yahoo (ex.: ASML.AS, SAP.DE, AAPL).")
    if t in _analises and not atualizar and time.time() - _analises[t]["_t"] < 3600:
        return _guardar_analisada(t, _analises[t]["r"], guardar)
    try:
        m = _motor()
        a = m.analisar_ticker(t, usar_cache=not atualizar)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"A Descoberta não conseguiu analisar {t}: {e}") from e
    val, qual, mo, ver = a["val"], a["qual"], a["moat"], a["ver"]
    r = limpo({
        "linha": m.linha_resumo(a), "gerado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "cenarios": [{k: c.get(k) for k in ("cenario", "valor", "probabilidade", "crescimento", "margem", "descricao") if k in c}
                     for c in val.get("cenarios", [])],
        "taxa_desconto": val.get("taxa_desconto"), "metodo": val.get("metodo"),
        "veredito": {"titulo": ver.get("titulo"), "acao": ver.get("acao"), "razoes": ver.get("razoes")},
        "alertas": [{"texto": x.get("texto") or x.get("titulo"), "gravidade": x.get("gravidade")} for x in qual.get("alertas", [])],
        "moat": {"titulo": mo.get("titulo"), "pontuacao": mo.get("pontuacao"), "tendencia": mo.get("tendencia_titulo"),
                 "fontes": [{"fonte": f.get("fonte"), "pontuacao": f.get("pontuacao")} for f in mo.get("fontes", [])]},
    })
    _analises[t] = {"_t": time.time(), "r": r}
    return _guardar_analisada(t, r, guardar)


def _guardar_analisada(t: str, r: dict, guardar: bool) -> dict:
    """As analisadas à mão ficam guardadas (e atualizam-se quando se volta a analisar)."""
    lista = _ler_analisadas()
    if (guardar and t not in lista) or (t in lista and lista[t].get("gerado") != r["gerado"]):
        lista[t] = {"linha": r["linha"], "gerado": r["gerado"]}
        ANALISADAS.parent.mkdir(parents=True, exist_ok=True)
        gravar_atomico(ANALISADAS, lista)
        _cache.pop("descoberta", None)
        _verif_criterios.pop(t, None)
    return r


# Empresas que procuraste e analisaste à mão na Pesquisa (fora das pesquisas guardadas): ficam neste ficheiro
ANALISADAS = Path(_env("CENTRO_DADOS") or AQUI / "dados") / "analisadas_por_mim.json"


def _ler_analisadas() -> dict:
    try:
        return json.loads(ANALISADAS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@app.delete("/api/pesquisa/analisadas/{ticker}")
def retirar_analisada(ticker: str):
    lista = _ler_analisadas()
    if lista.pop(ticker.strip().upper(), None) is None:
        raise HTTPException(404, "Essa empresa não está nas tuas analisadas.")
    gravar_atomico(ANALISADAS, lista)
    _cache.pop("descoberta", None)
    return {"ok": True}


_verif_criterios: dict = {}


@app.get("/api/pesquisa/criterios/{ticker}")
def pesquisa_criterios(ticker: str):
    """Que critérios das tuas pesquisas guardadas uma empresa cumpre, com as mesmas regras da procura automática:
    critérios do Yahoo pelo mesmo screener (a empresa aparece na consulta com o critério?), critérios depois da análise
    e moat pela linha do motor da Descoberta, e o universo (região da bolsa, sede, capitalização mínima)."""
    from concurrent.futures import ThreadPoolExecutor

    t = ticker.strip().upper()
    hit = _verif_criterios.get(t)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    pesq, uni, auto = _desc_mod("pesquisa"), _desc_mod("universo"), _desc_mod("automatico")
    import yfinance as yf
    from yfinance import EquityQuery as Q

    linha = pesquisa_analisar(t)["linha"]
    cot = next((c for c in (yf.Search(t, max_results=8, news_count=0).quotes or []) if (c.get("symbol") or "").upper() == t), {})
    bolsa = cot.get("exchange")
    reg = next((r for r, (_n, bolsas, _m) in uni.REGIOES.items() if bolsa in bolsas), None)
    try:
        mcap = float(yf.Ticker(t).fast_info["marketCap"] or 0)
    except Exception:  # noqa: BLE001
        mcap = 0.0

    def aparece(condicoes: list) -> bool | None:
        """A empresa aparece no screener do Yahoo com estas condições? (faixa de capitalização à volta da dela)"""
        if not reg or not mcap:
            return None
        nome_r, bolsas, _moeda = uni.REGIOES[reg]
        fator = uni.FATOR_LIMITE_SCREENER.get(reg, 1)
        filtros = [Q("eq", ["region", reg]), Q("is-in", ["exchange", *bolsas]),
                   Q("btwn", ["intradaymarketcap", mcap * fator * 0.75, mcap * fator * 1.25])]
        for campo, op, valor in condicoes:
            filtros.append(Q("btwn", [campo, 0, valor]) if op == "btwn" else Q(op, [campo, valor]))
        try:
            r = yf.screen(Q("and", filtros), size=250, sortField="intradaymarketcap", sortAsc=False)
        except Exception:  # noqa: BLE001
            return None
        return any((x.get("symbol") or "").upper() == t for x in (r or {}).get("quotes", []))

    base = aparece([])
    saida = {"ticker": t, "bolsa": bolsa, "regiao": uni.REGIOES[reg][0] if reg else None, "pais": linha.get("País"),
             "no_screener": base, "pesquisas": [], "gerado": datetime.now().strftime("%d/%m/%Y %H:%M")}
    for p in auto.listar():
        crit = p.get("criterios") or {}
        regioes = crit.get("regioes") or []
        total, minimo = pesq.contagem(crit)
        universo = [
            {"texto": "Bolsa numa das regiões da pesquisa", "cumpre": (reg in regioes) if reg else None,
             "detalhe": f"{bolsa or '?'} ({saida['regiao'] or 'região não reconhecida'})"},
            {"texto": "Sede num país aceite", "cumpre": (linha.get("País") in uni.paises_das_regioes(regioes)) if linha.get("País") else None,
             "detalhe": linha.get("País") or "lacuna"},
        ]
        try:
            moeda = uni.REGIOES[reg][2] if reg else None
            minimo_usd = float(crit.get("mcap", 0)) * 1e6
            mcap_usd = mcap / uni._cambio_para(moeda) if moeda and mcap else None
            universo.append({"texto": f"Capitalização ≥ {crit.get('mcap', 0):,.0f} M USD".replace(",", "."),
                             "cumpre": (mcap_usd >= minimo_usd) if mcap_usd else None,
                             "detalhe": f"{mcap_usd / 1e6:,.0f} M USD".replace(",", ".") if mcap_usd else "lacuna"})
        except Exception:  # noqa: BLE001
            universo.append({"texto": "Capitalização mínima", "cumpre": None, "detalhe": "sem câmbio"})
        ys = [(k, pesq._YAHOO[k], v) for k, v in (crit.get("yahoo") or {}).items() if k in pesq._YAHOO]
        if base:
            with ThreadPoolExecutor(max_workers=4) as ex:
                res_y = list(ex.map(lambda x: aparece([(x[1][3], x[1][4], x[2])]), ys))
        else:
            res_y = [None] * len(ys)
        criterios = [{"texto": pesq._texto_criterio(f, f[4], v), "cumpre": ok_, "origem": "Yahoo (screener)"} for (k, f, v), ok_ in zip(ys, res_y)]
        criterios += [{"texto": texto, "cumpre": bool(f(linha)), "origem": "motor da Descoberta"} for texto, f in pesq._criterios_depois(crit)]
        cumpridos = sum(1 for c in criterios if c["cumpre"])
        por_ver = sum(1 for c in criterios if c["cumpre"] is None)
        falhados = sum(1 for c in criterios if c["cumpre"] is False)
        if any(u["cumpre"] is False for u in universo) or falhados > total - minimo:
            passaria = False
        elif por_ver or any(u["cumpre"] is None for u in universo):
            passaria = None  # sem todos os dados não se pode dizer que passa
        else:
            passaria = True
        saida["pesquisas"].append({"nome": p.get("nome"), "universo": universo, "criterios": criterios, "total": total,
                                   "minimo": minimo, "cumpridos": cumpridos, "por_verificar": por_ver, "passaria": passaria})
    saida = limpo(saida)
    _verif_criterios[t] = (time.time(), saida)
    return saida


def _desc_mod(nome: str):
    if str(DESC_APP) not in sys.path:
        sys.path.insert(0, str(DESC_APP))
    import importlib
    return importlib.import_module(nome)


@app.get("/api/pesquisa/opcoes")
def pesquisa_opcoes():
    """Regiões, ordens, filtros do Yahoo e de análise, moat e classificações, lidos da própria Descoberta (um só sítio)."""
    filtros, universo, sintese = _desc_mod("filtros"), _desc_mod("universo"), _desc_mod("sintese")
    classes = []
    try:
        classes = [t for t in getattr(sintese, "TITULOS", {}).values()] or []
    except Exception:  # noqa: BLE001
        pass
    return {"regioes": {k: v[0] for k, v in universo.REGIOES.items()}, "ordens": universo.ORDENS,
            "setores": filtros.SETORES, "forca_moat": list(filtros.FORCA_MOAT), "tipos_moat": list(filtros.TIPOS_MOAT.values()),
            "classes": classes or ["Bom negócio a preço atrativo", "Bom negócio, mas caro", "Barato, mas com problemas",
                                   "Sem interesse para já", "Dados insuficientes"],
            "filtros": [{"grupo": f[0], "chave": f[1], "rotulo": f[2], "padrao": f[5], "min": f[6], "max": f[7], "passo": f[8],
                         "ativo": f[9], "ajuda": f[10] if len(f) > 10 else ""} for f in filtros.FILTROS_YAHOO],
            "filtros_analise": [{"grupo": f[0], "chave": f[1], "rotulo": f[2], "padrao": f[5], "min": f[6], "max": f[7], "passo": f[8],
                                 "ajuda": f[-1]} for f in filtros.FILTROS_ANALISE],
            **_opcoes_mediana(filtros)}


def _opcoes_mediana(filtros) -> dict:
    """Indústrias do screener do Yahoo por setor e as métricas que se podem comparar com a mediana (com o sentido)."""
    pesq = _desc_mod("pesquisa")
    try:
        from yfinance.const import EQUITY_SCREENER_EQ_MAP
        industrias = {s: sorted(v) for s, v in EQUITY_SCREENER_EQ_MAP.get("industry", {}).items()}
    except Exception:  # noqa: BLE001
        industrias = {}
    return {"industrias": industrias, "grupos_mediana": pesq.GRUPOS_MEDIANA, "min_empresas_omissao": pesq.MIN_EMPRESAS_OMISSAO,
            "metricas_mediana": [{"coluna": c, "grupo": g, "sentido": pesq.sentido(c), "ajuda": filtros.METRICAS.get(c, {}).get("ajuda", "")}
                                 for c, g in pesq.METRICAS_MEDIANA]}


@app.get("/api/pesquisas/medianas")
def pesquisas_medianas():
    """Da última procura de cada pesquisa com comparação à mediana: o grupo, as medianas e os valores de todas as
    candidatas (a página recalcula na hora quando mudas as métricas ou a percentagem)."""
    out = {}
    auto = DESCOBERTA / "automaticas"
    for f in sorted(auto.glob("*/medianas.json")) if auto.exists() else []:
        try:
            out[f.parent.name] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return limpo(out)


# ---------- As pesquisas guardadas do Rui (os critérios dele), as mesmas da Descoberta e do Agendador do Windows ----------
@app.get("/api/pesquisas")
def pesquisas_lista():
    auto, pesq = _desc_mod("automatico"), _desc_mod("pesquisa")
    out = []
    for p in auto.listar():
        crit = p.get("criterios") or {}
        total, minimo = pesq.contagem(crit)
        textos = [pesq._texto_criterio(pesq._YAHOO[k], pesq._YAHOO[k][4], v) for k, v in (crit.get("yahoo") or {}).items() if k in pesq._YAHOO]
        textos += [t for t, _f in pesq._criterios_depois(crit)]
        if pesq.metricas_mediana(crit):
            m = crit["mediana"]
            alvo = (m.get("industria") or "").replace("—", " - ") or "setor " + str(m.get("setor"))
            textos += [f"{c} melhor do que a mediana" for c in pesq.metricas_mediana(crit)] + [f"mediana: {alvo}, {pesq.GRUPOS_MEDIANA.get(m.get('grupo'), '')}"]
        out.append(limpo({**p, "descricao": pesq.descricao(crit), "criterios_texto": textos, "total_criterios": total, "minimo_criterios": minimo,
                          "agendamento": auto.agendamento(p["id"]), "registo": _registo_auto(p["id"])}))
    return out


def _registo_auto(ident: str) -> list:
    """Últimas linhas do registo das pesquisas automáticas desta pesquisa (com o erro, quando a última falhou)."""
    f = DESCOBERTA / "automaticas" / "registo.log"
    if not f.exists():
        return []
    linhas = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    blocos, atual = [], None
    for l in linhas:
        if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} ", l):
            atual = [l]
            blocos.append(atual)
        elif atual is not None:
            atual.append(l)
    meus = [b for b in blocos if f" {ident}:" in b[0]]
    return meus[-1][-8:] if meus else []


@app.get("/api/empresa/{ticker}")
def empresa(ticker: str):
    """Tudo o que o Centro sabe de uma empresa, para a ficha: análise da Decisão, relatório no disco e pedidos."""
    t = ticker.strip().upper()
    sync = ler_json(SYNC_DECISAO) or {}
    an = ((sync.get("decisao") or {}).get("analises") or {}).get(t) or {}
    res = decisao_resumo()["empresas"].get(t) or {}
    return limpo({"ticker": t, "resumo": res, "negocio": an.get("negocio"), "noticias": (an.get("noticias") or [])[:12],
                  "niveis": an.get("niveis"), "notas": an.get("notas"),
                  "pedidos": [p for p in pedidos_lista() if p.get("ticker") == t][:5]})


@app.put("/api/pesquisas/{ident}")
def pesquisa_guardar(ident: str, corpo: dict = Body(...)):
    """Grava os critérios no mesmo ficheiro da Descoberta (pesquisas/<id>.json): as duas apps e o Agendador usam-nos."""
    auto = _desc_mod("automatico")
    nome = str(corpo.get("nome") or "").strip()
    crit = corpo.get("criterios")
    if not nome or not isinstance(crit, dict) or not crit.get("regioes"):
        raise HTTPException(400, "Falta o nome, as regiões ou os critérios.")
    if ident != "nova" and auto.identificador(nome) != ident:
        raise HTTPException(400, "Para mudar o nome, cria uma pesquisa nova (o nome é o identificador da tarefa agendada).")
    novo = auto.guardar(nome, crit, list(corpo.get("alertar") or []))
    return {"id": novo}


@app.post("/api/pesquisas/{ident}/correr")
def pesquisa_correr(ident: str):
    """Corre a pesquisa guardada como o Agendador a corre (automatico.py), em segundo plano. «todas» = EUA e Europa."""
    if ident == "todas":
        return _lancar([sys.executable, "-u", "automatico.py", "--todas"], "todas as pesquisas guardadas")
    if not re.fullmatch(r"[a-z0-9\-]+", ident) or not _desc_mod("automatico").carregar(ident):
        raise HTTPException(404, "Pesquisa não encontrada.")
    return _lancar([sys.executable, "-u", "automatico.py", "--pesquisa", ident], "pesquisa guardada " + ident)


@app.post("/api/pesquisas/{ident}/agendar")
def pesquisa_agendar(ident: str, corpo: dict = Body(...)):
    """Autorizado pelo Rui a 10/10/2026 (pesquisas automáticas). Usa a mesma função da Descoberta (schtasks)."""
    auto = _desc_mod("automatico")
    if corpo.get("desligar"):
        ok_, msg = auto.desativar_agendamento(ident)
    else:
        if corpo.get("frequencia") not in ("diária", "dias úteis", "semanal") or not re.fullmatch(r"\d{2}:\d{2}", str(corpo.get("hora"))):
            raise HTTPException(400, "Frequência ou hora inválida.")
        ok_, msg = auto.ativar_agendamento(ident, corpo["frequencia"], corpo["hora"], corpo.get("dia") or "segunda")
    if not ok_:
        raise HTTPException(502, "O Agendador do Windows recusou: " + msg)
    return {"agendamento": auto.agendamento(ident)}


@app.post("/api/pesquisa/lote")
def pesquisa_lote(corpo: dict = Body(...)):
    """Corre o descobrir.py da Descoberta em segundo plano (pode demorar muitos minutos); o resultado entra na tabela."""
    args = [sys.executable, "-u", "descobrir.py", "--regioes", str(corpo.get("regioes") or "us"),
            "--mcap", str(float(corpo.get("mcap") or 2000)), "--ordem", str(int(corpo.get("ordem") or 0))]
    if corpo.get("limite"):
        args += ["--limite", str(int(corpo["limite"]))]
    if corpo.get("setores"):
        args += ["--setores", ",".join(corpo["setores"])]
    for k, v in (corpo.get("filtros") or {}).items():
        if re.fullmatch(r"[a-z_]+", k) and v not in (None, ""):
            args += ["--" + k.replace("_", "-"), str(float(v))]
    return _lancar(args, " ".join(args[2:]))


def _lancar(args: list, descricao: str) -> dict:
    """Uma pesquisa de cada vez, em segundo plano, com o registo em pesquisa_lote.log."""
    import subprocess
    if _lote["proc"] and _lote["proc"].poll() is None:
        raise HTTPException(409, "Já há uma pesquisa a correr. Espera que acabe ou para-a.")
    log = open(LOG_LOTE, "w", encoding="utf-8")
    _lote.update(proc=subprocess.Popen(args, cwd=DESC_APP, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                 inicio=datetime.now().strftime("%d/%m %H:%M"), args=descricao)
    return pesquisa_lote_estado()


@app.get("/api/pesquisa/lote")
def pesquisa_lote_estado():
    p = _lote["proc"]
    a_correr = bool(p and p.poll() is None)
    if p and not a_correr:
        _cache.pop("descoberta", None)  # resultados novos entram na tabela
    linhas = LOG_LOTE.read_text(encoding="utf-8", errors="ignore").splitlines()[-12:] if LOG_LOTE.exists() else []
    return {"a_correr": a_correr, "inicio": _lote["inicio"], "args": _lote["args"],
            "codigo": None if a_correr or not p else p.returncode, "registo": linhas}


@app.post("/api/pesquisa/lote/parar")
def pesquisa_lote_parar():
    p = _lote["proc"]
    if p and p.poll() is None:
        p.terminate()
    return pesquisa_lote_estado()


# ---------------------------------------------------------------------------------------------------------------
# Relatórios: estado de cada empresa face aos resultados publicados, com os pedidos ao Cowork ao lado
# ---------------------------------------------------------------------------------------------------------------
@app.get("/api/relatorios/estado")
def relatorios_estado():
    try:
        cal = http_json(DECISAO + "/api/calendario", 120).get("empresas", [])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "A Decisão de Investimento (8600) não responde: sem calendário de resultados.") from e
    pedidos = [p for p in pedidos_lista() if p.get("estado") in ("pendente", "em_curso", "bloqueado")]
    out = []
    for c in cal:
        t = c["ticker"]
        try:
            ult = ultimo_relatorio(t)
            base = baseline_de(t) if ult else None
        except Exception:  # noqa: BLE001
            ult, base = None, None
        out.append(limpo({
            "ticker": t, "nome": c.get("nome"), "lista": c.get("lista"),
            "ultimos_resultados": (c.get("ultimo") or {}).get("data"), "proximos_resultados": (c.get("proximo") or {}).get("data"),
            "dias": (c.get("proximo") or {}).get("dias"), "estado_relatorio": (c.get("relatorio") or {}).get("estado"),
            "texto_relatorio": (c.get("relatorio") or {}).get("texto"),
            "ultimo": {"ficheiro": ult.get("caminho"), "data": ult.get("data"), "tipo": ult.get("tipo"), "periodo": ult.get("periodo"),
                       "formato": formato_relatorio(ult.get("caminho"))} if ult else None,
            "baseline_ok": bool(base and base.get("tem_resumo_baseline")), "titulo_resumo": base.get("titulo_resumo") if base else None,
            "pedidos": [{"estado": p["estado"], "tipo": p["tipo"], "ficheiro": p["_ficheiro"], "motivo": p.get("motivo")}
                        for p in pedidos if p.get("ticker") == t],
        }))
    return {"empresas": out, "gerado": datetime.now().strftime("%d/%m/%Y %H:%M")}


app.mount("/web", StaticFiles(directory=AQUI / "web"), name="web")

if __name__ == "__main__":
    import uvicorn
    porta = int(sys.argv[sys.argv.index("--porta") + 1]) if "--porta" in sys.argv else PORTA
    uvicorn.run(app, host="127.0.0.1", port=porta, log_level="warning")
