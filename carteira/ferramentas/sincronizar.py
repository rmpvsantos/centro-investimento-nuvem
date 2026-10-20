"""
Gera sincronizacao_carteira.json para carregares na app Carteira (Ferramentas -> Sincronização):

  1. Relatórios: recomendação, preço-alvo, tese e fosso de cada empresa (as mesmas regras da Decisão de Investimento).
  2. Decisão de Investimento: a análise completa de cada empresa da carteira e da observação (sinal e pilares, valor de
     referência e preços de entrada, técnica, gestão, moat, triagem, próximos resultados, negócio, notícias, notas).
  3. Listas: carteira e observação (a observação passa a ser a watchlist da app Carteira).

Reutiliza o código da Decisão de Investimento (pasta servidor). Só lê; não altera nada nessa app nem nos relatórios.
A primeira análise de cada empresa demora 1 a 2 minutos (Yahoo + concorrentes); corre 4 em paralelo.

Correr: "Sincronizar com a Decisão de Investimento.bat".   Só relatórios (rápido): ... sincronizar.py --so-relatorios
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

DECISAO = Path(os.environ.get("DECISAO_DIR") or Path.home() / "Documents" / "decisao_investimento")  # na nuvem: DECISAO_DIR
sys.path.insert(0, str(DECISAO / "servidor"))

import wiki  # noqa: E402  (vem da Decisão de Investimento)

# na nuvem: o ficheiro que veio do Drive (a técnica rápida tira dele as listas)
SAIDA = Path(os.environ.get("SINC_SAIDA") or Path(__file__).resolve().parent.parent / "sincronizacao_carteira.json")

# Ticker dos relatórios / Yahoo -> ticker da folha, quando são diferentes
ALIAS = {"RELX": "LON:REL", "NVO": "ETR:NOV", "VISA": "V"}
# Sufixo do Yahoo -> prefixo do Google Finance (para a watchlist ter cotação ao vivo na folha)
YAHOO_GOOGLE = {".ST": "STO:", ".L": "LON:", ".DE": "ETR:", ".AS": "AMS:", ".PA": "EPA:", ".CO": "CPH:", ".SW": "SWX:",
                ".MC": "BME:", ".MI": "BIT:", ".LS": "ELI:", ".OL": "OSL:", ".HE": "HEL:", ".TO": "TSE:", ".BR": "EBR:"}
TESE = {"quebrada": "quebrada", "enfraquecida": "enfraquece", "reforcada": "reforça-se", "intacta": "mantém-se"}
ORDEM = {"carteira": 0, "analise": 1, "varios": 2}


def ticker_folha(t: str) -> str:
    return ALIAS.get(t.upper(), t.upper())


def ticker_google(t: str) -> str:
    t = ticker_folha(t)
    for suf, pref in YAHOO_GOOGLE.items():
        if t.endswith(suf):
            return pref + t[: -len(suf)].replace("-", ".")
    return t


def moeda_do_excerto(excerto: str) -> str | None:
    """Moeda logo a seguir ao preço-alvo no excerto (o wiki.py guarda o valor mas não a moeda)."""
    m = re.search(r"(?i)pre[çc]o[- ]alvo(.{0,90})", excerto or "")
    janela = m.group(1) if m else (excerto or "")
    for padrao, moeda in ((r"GBp|pence|\d\s*p\b", "GBp"), (r"US\$|\$|USD|d[óo]lar", "USD"), (r"€|EUR|euro", "EUR"),
                          (r"£|GBP|libra", "GBP"), (r"SEK|coroas? suecas?", "SEK"), (r"DKK|coroas? dinamarquesas?", "DKK"),
                          (r"CHF|franco", "CHF"), (r"CAD", "CAD")):
        if re.search(padrao, janela):
            return moeda
    return None


def relatorios(indice: dict) -> dict:
    empresas: dict[str, dict] = {}
    for e in sorted(indice["empresas"], key=lambda x: ORDEM.get(x["categoria"], 9)):
        if e["categoria"] not in ORDEM:
            continue
        ticker = ticker_folha(e["ticker"])
        if ticker in empresas:
            continue
        k = wiki.conhecimento(indice, e["ticker"])
        alvo = k.get("preco_alvo") or {}
        valor, moeda = alvo.get("valor"), moeda_do_excerto(alvo.get("excerto"))
        if valor and moeda == "GBp":
            valor, moeda = valor / 100, "GBP"
        ultimo = (k.get("historico") or [{}])[-1]
        tese = (k.get("tese") or {}).get("valor")
        empresas[ticker] = {
            "nome": e["nome"], "lista": e["categoria"],
            "recomendacao": (k.get("recomendacao") or {}).get("valor"),
            "precoAlvo": valor, "moedaAlvo": moeda, "excertoAlvo": alvo.get("excerto"),
            "tese": TESE.get(tese, tese), "excertoTese": (k.get("tese") or {}).get("excerto"),
            "fosso": (k.get("fosso") or {}).get("valor"),
            "relatorio": ultimo.get("titulo"), "ficheiro": ultimo.get("id"), "data": ultimo.get("data"),
            "periodo": ultimo.get("periodo"),
            "historico": [{"titulo": r.get("titulo"), "data": r.get("data"), "periodo": r.get("periodo"), "tipo": r.get("tipo"),
                           "recomendacao": r.get("recomendacao"), "precoAlvo": r.get("preco_alvo"), "tese": TESE.get(r.get("veredito"), r.get("veredito"))}
                          for r in (k.get("historico") or [])][-8:],
        }
    return {"gerado": datetime.now().strftime("%d/%m/%Y %H:%M"), "empresas": empresas}


FOLHA_YAHOO = {v: k for k, v in ALIAS.items() if k != "VISA"}  # LON:REL -> RELX, ETR:NOV -> NVO
GOOGLE_YAHOO = {v: k for k, v in YAHOO_GOOGLE.items()}


def ticker_yahoo(t: str) -> str:
    """Ticker da folha (Google Finance) -> Yahoo, para analisar na Decisão de Investimento."""
    if t in FOLHA_YAHOO:
        return FOLHA_YAHOO[t]
    if ":" in t:
        pref, base = t.split(":", 1)
        return base.replace(".", "-") + GOOGLE_YAHOO.get(pref + ":", "")
    return t


def analisar_http(t: str) -> dict:
    """O mesmo que analisar() abaixo, mas pela Análise que está a correr (modo nuvem)."""
    from urllib.parse import quote
    q = quote(t.upper(), safe="")
    a = _api(f"/api/empresa/{q}/decisao", 900)
    neg = a.get("negocio") or {}
    neg.pop("original", None)
    try:
        nts = _api(f"/api/empresa/{q}/noticias", 300)
        nts = nts.get("noticias", []) if isinstance(nts, dict) else nts
        a["noticias"] = [{k: n.get(k) for k in ("data", "titulo", "resumo", "fonte", "url")} for n in (nts or [])[:6]]
    except Exception:  # noqa: BLE001
        a["noticias"] = []
    try:
        a["niveis"] = _api(f"/api/empresa/{q}/tecnica", 300).get("niveis")
    except Exception:  # noqa: BLE001
        a["niveis"] = None
    try:
        a["notas"] = _api(f"/api/wiki/{q}", 300).get("notas") or {}
    except Exception:  # noqa: BLE001
        a["notas"] = {}
    return a


def decisao(indice: dict, extra: list[str] | None = None) -> dict:
    import listas as L  # noqa: E402
    import noticias as N  # noqa: E402
    if not NA_NUVEM:
        import main as D  # noqa: E402  (cria a app FastAPI sem a pôr a correr; só usamos as funções)

    ls = L.ler(indice)
    alvo = [(lista, x) for lista in ("carteira", "observacao") for x in ls.get(lista, [])]
    # Ações que tens na folha mas que não estão em nenhuma lista da Decisão: analisa também
    vistos = {wiki.normalizar(ticker_folha(x["ticker"])).split(".")[0] for _, x in alvo}
    for t in extra or []:
        if wiki.normalizar(t.split(":")[-1]).split(".")[0] not in vistos:
            alvo.append(("folha", {"ticker": ticker_yahoo(t), "nome": t}))
    analises, erros = {}, {}

    def analisar(t: str) -> dict:
        if NA_NUVEM:  # na nuvem: a Análise já está a correr (e com as caches quentes); pede-se-lhe por HTTP
            return analisar_http(t)
        a = D.limpo(D.bloco_decisao(t))
        neg = a.get("negocio") or {}
        neg.pop("original", None)  # o texto em inglês só ocupa espaço
        a["noticias"] = [{k: n.get(k) for k in ("data", "titulo", "resumo", "fonte", "url")} for n in N.lista(t, 6)]
        try:  # suportes e resistências (o histórico de preços já está em cache depois do bloco_decisao)
            a["niveis"] = D.limpo(D.bloco_tecnica(t)).get("niveis")
        except Exception:  # noqa: BLE001
            a["niveis"] = None
        a["notas"] = wiki.ler_notas(t)
        return a

    inicio = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futuros = {ex.submit(analisar, x["ticker"]): x["ticker"] for _, x in alvo}
        for i, f in enumerate(as_completed(futuros), 1):
            t = futuros[f]
            try:
                analises[ticker_folha(t)] = f.result()
                d = analises[ticker_folha(t)].get("decisao") or {}
                print(f"  [{i}/{len(futuros)}] {t:9} {d.get('rotulo', '-')}  ({time.time() - inicio:.0f}s)", flush=True)
            except Exception as e:  # noqa: BLE001
                erros[t] = f"{type(e).__name__}: {getattr(e, 'detail', e)}"
                print(f"  [{i}/{len(futuros)}] {t:9} ERRO {erros[t]}", flush=True)

    def item(x: dict) -> dict:
        return {"ticker": x["ticker"], "tickerFolha": ticker_folha(x["ticker"]), "tickerGoogle": ticker_google(x["ticker"]),
                "nome": x.get("nome"), "desde": x.get("desde")}

    return {"gerado": datetime.now().strftime("%d/%m/%Y %H:%M"), "analises": analises, "erros": erros,
            "listas": {k: [item(x) for x in ls.get(k, [])] for k in ("carteira", "observacao")}}


# ---------------------------------------------------------------------------------------------------------------------
# Pasta partilhada com a app (Google Drive para computador) e modo automático
# ---------------------------------------------------------------------------------------------------------------------
PASTA_SYNC = "Carteira - sincronização"
# Modo nuvem (trabalhador do Centro de Investimento no GitHub): a Análise já está a correr e o estado fica com os dados
NA_NUVEM = bool(os.environ.get("SINC_NA_NUVEM"))
ESTADO = Path(os.environ.get("SINC_ESTADO") or Path(__file__).resolve().parent / "estado_sincronizacao.json")
REGISTO = Path(__file__).resolve().parent / "sincronizacao.log"
HORAS_ENTRE_COMPLETAS = 6
DESTINO = {"watchlist": "observacao", "carteira": "carteira", "retirar": "outros"}


def pasta_drive() -> Path | None:
    """A pasta 'Carteira - sincronização' dentro do Google Drive do PC (unidade G: ou pasta espelhada)."""
    if os.environ.get("SINC_PASTA"):  # na nuvem: uma pasta local que o trabalhador troca com o Google Drive
        p = Path(os.environ["SINC_PASTA"])
        p.mkdir(parents=True, exist_ok=True)
        return p
    candidatos = []
    for letra in "GHIJKLMNOPQRSTUVWXYZDEF":
        for nome in ("O meu disco", "My Drive", "A minha unidade"):
            candidatos.append(Path(f"{letra}:/") / nome)
    for nome in ("O meu disco", "My Drive", "Google Drive"):
        candidatos.append(Path.home() / nome)
    for base in candidatos:
        try:
            if base.is_dir():
                p = base / PASTA_SYNC
                p.mkdir(exist_ok=True)
                return p
        except OSError:
            continue
    return None


def registar(msg: str) -> None:
    linha = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linha, flush=True)
    try:
        antigas = REGISTO.read_text(encoding="utf-8").splitlines()[-400:] if REGISTO.exists() else []
        REGISTO.write_text("\n".join(antigas + [linha]) + "\n", encoding="utf-8")
    except OSError:
        pass


def ler_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ultima_completa": 0, "tratados": []}


def mover_http(destino: str, ticker: str) -> None:
    """Modo nuvem: muda a lista pela Análise que está a correr; a mudança de pasta fica registada para o PC a fazer."""
    import urllib.error
    import urllib.request
    from fastapi import HTTPException  # noqa: E402
    req = urllib.request.Request(f"{SERVIDOR_DECISAO}/api/listas/{destino}", data=json.dumps({"ticker": ticker}).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, json.loads(e.read().decode("utf-8") or "{}").get("detail", str(e))) from e
    if res.get("movida") and os.environ.get("SINC_MUDANCAS"):
        f = Path(os.environ["SINC_MUDANCAS"])
        mud = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        mud.append({**res["movida"], "quando": datetime.now().isoformat(timespec="seconds")})
        f.write_text(json.dumps(mud, ensure_ascii=False, indent=1), encoding="utf-8")


def tratar_pedidos(pedidos: list[dict], estado: dict) -> list[dict]:
    """Executa na Decisão de Investimento os pedidos feitos na app (mesmo efeito dos botões «Mover para…»)."""
    from fastapi import HTTPException  # noqa: E402

    if not NA_NUVEM:
        import main as D  # noqa: E402
    resultados = []
    for p in pedidos:
        if p["id"] in estado["tratados"]:
            continue
        destino = DESTINO.get(p.get("acao"))
        try:
            if not destino:
                raise ValueError(f"pedido desconhecido: {p.get('acao')}")
            if NA_NUVEM:
                mover_http(destino, p["ticker"])
            else:
                D.api_listas_mover(destino, {"ticker": p["ticker"]})
            nome = {"observacao": "watchlist (Em observação)", "carteira": "carteira", "outros": "fora das listas"}[destino]
            resultados.append({"id": p["id"], "ok": True, "mensagem": f"{p['ticker']} passou para {nome}."})
        except HTTPException as e:
            resultados.append({"id": p["id"], "ok": False, "mensagem": str(e.detail)})
        except Exception as e:  # noqa: BLE001
            resultados.append({"id": p["id"], "ok": False, "mensagem": f"{type(e).__name__}: {e}"})
        registar(f"Pedido {p['acao']} {p['ticker']}: {resultados[-1]['mensagem']}")
        estado["tratados"] = (estado["tratados"] + [p["id"]])[-500:]
    return resultados


def sincronizar(extra: list[str] | None = None, tratados: list[dict] | None = None, so_relatorios: bool = False) -> dict:
    indice = wiki.construir_indice()  # depois de tratar os pedidos: as pastas podem ter mudado de lista
    saida = {"gerado": datetime.now().strftime("%d/%m/%Y %H:%M"), "relatorios": relatorios(indice), "pedidosTratados": tratados or []}
    registar(f"Relatórios: {len(saida['relatorios']['empresas'])} empresas.")
    if not so_relatorios:
        registar("A analisar a carteira e a observação na Decisão de Investimento…")
        saida["decisao"] = decisao(indice, extra)
        registar(f"Decisão: {len(saida['decisao']['analises'])} análises, {len(saida['decisao']['erros'])} erros.")
    return saida


def gravar(saida: dict, pasta: Path | None) -> None:
    texto = json.dumps(saida, ensure_ascii=False)
    SAIDA.write_text(texto, encoding="utf-8")
    if pasta:
        tmp = pasta / ("~" + SAIDA.name)
        tmp.write_text(texto, encoding="utf-8")
        tmp.replace(pasta / SAIDA.name)  # troca de uma vez: o Drive nunca envia um ficheiro a meio
        registar(f"Enviado para o Google Drive: {pasta / SAIDA.name} ({len(texto) / 1024:.0f} KB)")
    else:
        registar(f"Gravado: {SAIDA}. Google Drive para computador não encontrado: carrega o ficheiro na app à mão.")


# ---------------------------------------------------------------------------------------------------------------------
# Triagem a pedido (pesquisa da app Carteira): usa a Decisão de Investimento que está a correr no PC (e a cache dela)
# ---------------------------------------------------------------------------------------------------------------------
TRIAGENS = "triagens_carteira.json"
SERVIDOR_DECISAO = os.environ.get("SINC_DECISAO_URL") or "http://127.0.0.1:8600"
CAMPOS_CRITERIO = ("criterio", "estado", "detalhe", "valor", "tipo", "limiar", "mediana_grupo", "n_grupo", "face_grupo", "desativado",
                   "eliminatorio", "personalizado")


def _api(caminho: str, timeout: int) -> dict:
    import urllib.request
    with urllib.request.urlopen(SERVIDOR_DECISAO + caminho, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def triagem(ticker: str) -> dict:
    """Triagem como na Decisão de Investimento (critérios, veredicto, concorrentes) e o resumo do negócio."""
    from urllib.parse import quote
    t = quote(ticker.upper(), safe="")
    try:
        tri = _api(f"/api/empresa/{t}/triagem", 300)
    except Exception:  # noqa: BLE001  (servidor desligado: corre aqui mesmo)
        import main as D  # noqa: E402
        tri = D.limpo(D.bloco_triagem(ticker.upper()))
    neg = {}
    try:
        neg = _api(f"/api/empresa/{t}/negocio", 90)
    except Exception:  # noqa: BLE001
        pass
    return {
        "ticker": tri.get("ticker"), "nome": tri.get("nome"), "setor": tri.get("setor"), "industria": tri.get("industria"),
        "moeda": tri.get("moeda"), "preco": tri.get("preco"), "capitalizacao": tri.get("capitalizacao"), "periodo": tri.get("periodo"),
        "pe_atual": tri.get("pe_atual"), "pe_mediana": tri.get("pe_mediana"),
        "criterios": [{k: c.get(k) for k in CAMPOS_CRITERIO} for c in tri.get("criterios", [])],
        "score": tri.get("score"), "score_max": tri.get("score_max"), "veredicto": tri.get("veredicto"),
        "avisos": tri.get("avisos", []), "concorrentes": (tri.get("grupo") or {}).get("empresas", []),
        "base_concorrentes": (tri.get("grupo") or {}).get("base"),
        "negocio": {k: neg.get(k) for k in ("texto", "site", "pais", "setor", "industria", "funcionarios")} if neg else None,
    }


def aplicar_pares(ticker: str, pares: list[str]) -> None:
    """Grava os concorrentes escolhidos na Decisão de Investimento (o mesmo que «Grupo de comparação» na Triagem dela)
    e limpa a cache dessa empresa. Lista vazia = volta aos concorrentes automáticos."""
    import urllib.request
    from urllib.parse import quote
    t = quote(ticker.upper(), safe="")
    try:
        corpo = json.dumps({"pares": pares}).encode("utf-8")
        req = urllib.request.Request(f"{SERVIDOR_DECISAO}/api/empresa/{t}/notas", data=corpo, method="PUT",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        urllib.request.urlopen(urllib.request.Request(f"{SERVIDOR_DECISAO}/api/renovar/{t}", data=b"", method="POST"), timeout=30).read()
    except Exception:  # noqa: BLE001  (servidor desligado: grava diretamente nas notas)
        wiki.guardar_notas(ticker.upper(), {"pares": pares})


def tratar_triagens(pedidos: list[dict], estado: dict, pasta: Path) -> int:
    ficheiro = pasta / TRIAGENS
    try:
        tri = json.loads(ficheiro.read_text(encoding="utf-8")) if ficheiro.exists() else {}
    except (OSError, ValueError):
        tri = {}
    n = 0
    for p in pedidos:
        t = p["ticker"].upper()
        try:
            if p.get("acao") == "pares":
                pares = ((p.get("dados") or {}).get("pares")) or []
                aplicar_pares(t, pares)
                registar(f"Concorrentes de {t}: {', '.join(pares) if pares else 'automáticos'}")
                estado["forcar_completa"] = True  # a análise completa (se estiver numa lista) também passa a usá-los
            dados = triagem(t)
            tri[t] = {"pedido": p["id"], "gerado": datetime.now().strftime("%Y-%m-%d %H:%M"), "ok": True, "dados": dados}
            v = (dados.get("veredicto") or {}).get("titulo", "")
            registar(f"Triagem {t}: {dados.get('score')}/{dados.get('score_max')} — {v}")
        except Exception as e:  # noqa: BLE001
            msg = str(getattr(e, "detail", "") or e)
            if "404" in msg or "Sem dados" in msg:
                msg = f"Sem dados no Yahoo para '{t}'. Confirma o ticker (os europeus levam o sufixo da bolsa)."
            tri[t] = {"pedido": p["id"], "gerado": datetime.now().strftime("%Y-%m-%d %H:%M"), "ok": False, "erro": msg}
            registar(f"Triagem {t}: erro {msg}")
        estado["tratados"] = (estado["tratados"] + [p["id"]])[-500:]
        n += 1
        # grava depois de cada uma, para a app ver os resultados assim que estejam prontos
        mais_recentes = dict(sorted(tri.items(), key=lambda kv: kv[1].get("gerado", ""), reverse=True)[:80])
        tmp = pasta / ("~" + TRIAGENS)
        tmp.write_text(json.dumps(mais_recentes, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ficheiro)
        ESTADO.write_text(json.dumps(estado), encoding="utf-8")
    return n


# ---------------------------------------------------------------------------------------------------------------------
# Técnica rápida (de 15 em 15 minutos): suportes, resistências, RSI, médias e MACD, sem refazer a análise completa.
# Vai também o «estado» do RSI de Wilder (médias de ganhos e perdas) nos dois últimos fechos, para a app o
# recalcular com o preço de agora (um passo de Wilder com o preço atual dá o mesmo RSI que a Decisão daria).
# ---------------------------------------------------------------------------------------------------------------------
TECNICA = "tecnica_carteira.json"
MINUTOS_TECNICA = 15


def estados_rsi(fechos: list[float], periodo: int = 14) -> list[tuple[float, float]]:
    """(ganho médio, perda média) de Wilder depois de cada fecho (igual a motor/tecnica.py da Decisão)."""
    est: list[tuple[float, float]] = [(float("nan"), float("nan"))] * len(fechos)
    if len(fechos) <= periodo:
        return est
    difs = [fechos[i] - fechos[i - 1] for i in range(1, periodo + 1)]
    g = sum(d for d in difs if d > 0) / periodo
    p = sum(-d for d in difs if d < 0) / periodo
    est[periodo] = (g, p)
    for i in range(periodo + 1, len(fechos)):
        d = fechos[i] - fechos[i - 1]
        g = (g * (periodo - 1) + max(d, 0)) / periodo
        p = (p * (periodo - 1) + max(-d, 0)) / periodo
        est[i] = (g, p)
    return est


def tecnica_rapida(yahoo: str) -> dict:
    from urllib.parse import quote
    t = _api(f"/api/empresa/{quote(yahoo.upper(), safe='')}/tecnica", 120)
    barras = t.get("barras") or []
    fechos = [b["f"] for b in barras]
    est = estados_rsi(fechos)
    ultimos = [{"data": barras[i]["t"], "fecho": fechos[i], "ganho": est[i][0], "perda": est[i][1]}
               for i in range(max(0, len(barras) - 3), len(barras))]
    u = t.get("ultimo") or {}
    return {"ticker": yahoo.upper(), "moeda": t.get("moeda"), "preco": t.get("preco"), "tendencia": t.get("tendencia"),
            "rsi": u.get("rsi"), "mm50": u.get("mm50"), "mm200": u.get("mm200"), "macd": u.get("macd"),
            "macd_sinal": u.get("macd_sinal"), "cruzamento_macd": u.get("cruzamento_macd"), "niveis": t.get("niveis"),
            "estados": ultimos, "fechos": [[barras[i]["t"], fechos[i]] for i in range(max(0, len(barras) - 130), len(barras))]}


def atualizar_tecnica(pasta: Path, carteira: list[str]) -> None:
    """Escreve tecnica_carteira.json com a técnica de todas as ações da carteira e da watchlist."""
    alvo: dict[str, str] = {}  # ticker da folha -> símbolo do Yahoo
    try:
        sync = json.loads(SAIDA.read_text(encoding="utf-8"))
        for lista in ("carteira", "observacao"):
            for x in ((sync.get("decisao") or {}).get("listas") or {}).get(lista, []):
                alvo[x["tickerFolha"]] = x["ticker"]
    except (OSError, ValueError):
        pass
    for t in carteira:
        if not any(wiki.normalizar(k.split(":")[-1]).split(".")[0] == wiki.normalizar(t.split(":")[-1]).split(".")[0] for k in alvo):
            alvo[t] = ticker_yahoo(t)
    # Ações de bolsas fora dos EUA: a técnica tem de vir da mesma bolsa da cotação da folha (LON:REL -> REL.L, não o ADR RELX)
    for folha in list(alvo):
        if ":" in folha:
            pref, base = folha.split(":", 1)
            alvo[folha] = base.replace(".", "-") + GOOGLE_YAHOO.get(pref + ":", "")
    ativos, erros = {}, {}
    for folha, yahoo in alvo.items():
        try:
            ativos[folha] = tecnica_rapida(yahoo)
        except Exception as e:  # noqa: BLE001
            erros[folha] = str(e)[:200]
    saida = {"gerado": datetime.now().strftime("%Y-%m-%d %H:%M"), "ativos": ativos, "erros": erros}
    tmp = pasta / ("~" + TECNICA)
    tmp.write_text(json.dumps(saida, ensure_ascii=False), encoding="utf-8")
    tmp.replace(pasta / TECNICA)


# ---------------------------------------------------------------------------------------------------------------------
# Uma volta de trabalho (usada pelo vigilante de 15 em 15 segundos e pela tarefa de 15 em 15 minutos)
# ---------------------------------------------------------------------------------------------------------------------
TRANCA = Path(__file__).resolve().parent / "vigilante.lock"


def ler_pedidos(pasta: Path | None) -> tuple[list[dict], list[str]]:
    if not pasta or not (pasta / "pedidos_carteira.json").exists():
        return [], []
    try:
        pj = json.loads((pasta / "pedidos_carteira.json").read_text(encoding="utf-8"))
        return pj.get("pedidos", []), pj.get("carteira", [])
    except (OSError, ValueError) as e:
        registar(f"Não consegui ler os pedidos: {e}")
        return [], []


def listas_mudaram(estado: dict) -> bool:
    """Mudaste as listas na própria Decisão de Investimento (acrescentar, mover, retirar)?"""
    try:
        from config import pasta_dados  # noqa: E402  (da Decisão de Investimento)
        if NA_NUVEM:  # na nuvem o ficheiro é recriado em cada volta: conta o conteúdo, não a data
            import hashlib
            return hashlib.sha1((pasta_dados() / "listas.json").read_bytes()).hexdigest() != estado.get("listas_hash")
        return (pasta_dados() / "listas.json").stat().st_mtime > estado.get("ultima_completa", 0)
    except (ImportError, OSError):
        return False


# ---------------------------------------------------------------------------------------------------------------------
# Da nuvem para o PC (só no PC): o Centro de Investimento na internet guarda os dados na pasta «Centro - nuvem» do
# Google Drive. Os pedidos de relatório feitos lá passam para a pasta _PEDIDOS (que o Cowork lê), o estado que o Cowork
# lá escreve volta para a nuvem, e as mudanças de lista feitas lá mudam a pasta do relatório no OneDrive (o mesmo que o
# botão «Mover para…» da Decisão faz no PC).
# ---------------------------------------------------------------------------------------------------------------------
ESTUDOS = Path.home() / "OneDrive" / "Ambiente de Trabalho" / "Estudos Empresas-Relatorios"
# De 17 a 20/10/2026 a Carteira, a técnica rápida e as triagens calcularam-se só na nuvem (SO_PONTE_COWORK = True).
# A 20/10/2026 o Rui decidiu voltar ao PC (a nuvem era lenta e falhava): o vigilante volta a fazer tudo no PC e a nuvem
# fica só para ver no telemóvel (recebe os dados do PC em para_nuvem).
SO_PONTE_COWORK = False


def da_nuvem(pasta: Path | None, estado: dict) -> None:
    import base64
    import gzip
    import hashlib
    import shutil

    nuvem = pasta.parent / "Centro - nuvem" if pasta else None
    pacote = nuvem / "dados_centro.json" if nuvem else None
    if not pacote or not pacote.exists():
        return
    pedidos = ESTUDOS / "_PEDIDOS"
    mtime = pacote.stat().st_mtime
    # com a nuvem só para ver (desde 20/10/2026), de lá não vêm pedidos nem mudanças de pasta: o pacote é escrito pelo PC
    if SO_PONTE_COWORK and estado.get("nuvem_mtime") != mtime:
        ficheiros = json.loads(pacote.read_text(encoding="utf-8")).get("ficheiros", {})
        ler = lambda k: gzip.decompress(base64.b64decode(ficheiros[k]))  # noqa: E731
        for rel in ficheiros:
            if rel.startswith("pedidos_cowork/") and rel.endswith(".json"):
                alvo = pedidos / rel.split("/", 1)[1]
                if not alvo.exists():  # pedido novo feito na nuvem; os que já cá estão são do Cowork e ficam
                    pedidos.mkdir(parents=True, exist_ok=True)
                    tmp = alvo.with_name(alvo.name + ".tmp")
                    tmp.write_bytes(ler(rel))
                    tmp.replace(alvo)
                    registar(f"Pedido ao Cowork vindo da nuvem: {alvo.name}")
        if "mudancas_pastas.json" in ficheiros:
            for m in json.loads(ler("mudancas_pastas.json").decode("utf-8")):
                de, para = ESTUDOS / m["de"].replace("\\", "/"), ESTUDOS / m["para"].replace("\\", "/")
                if de.is_dir() and not para.exists() and para.parent.is_dir():
                    shutil.move(str(de), str(para))
                    registar(f"Pasta mudada (lista mudada na nuvem): {m['de']} -> {m['para']}")
        estado["nuvem_mtime"] = mtime
    # O estado dos pedidos ao Cowork (o Cowork escreve-o nos ficheiros de _PEDIDOS) volta para a nuvem quando muda
    if pedidos.is_dir():
        atual = {f.name: f.read_text(encoding="utf-8", errors="replace") for f in sorted(pedidos.glob("*.json"))}
        texto = json.dumps(atual, ensure_ascii=False, sort_keys=True)
        h = hashlib.sha1(texto.encode("utf-8")).hexdigest()
        if estado.get("cowork_hash") != h:
            tmp = nuvem / "~cowork_estado.json"
            tmp.write_text(texto, encoding="utf-8")
            tmp.replace(nuvem / "cowork_estado.json")
            estado["cowork_hash"] = h
    # Lista dos relatórios do PC (caminho e data), quando muda: a nuvem compara-a com a cópia do Google Drive e avisa
    # se algum relatório do Cowork ainda não chegou lá (ou chegou numa versão mais antiga)
    rel = {}
    for c in ("CARTEIRA INVESTIMENTOS", "EM OBSERVAÇÃO", "ANALISES ANTIGAS"):
        for f in (ESTUDOS / c).rglob("*"):
            if f.is_file() and f.suffix.lower() in (".html", ".htm"):
                rel[f.relative_to(ESTUDOS).as_posix()] = int(f.stat().st_mtime)
    texto_rel = json.dumps(rel, ensure_ascii=False, sort_keys=True)
    h_rel = hashlib.sha1(texto_rel.encode("utf-8")).hexdigest()
    if rel and estado.get("relatorios_hash") != h_rel:
        from datetime import datetime
        tmp = nuvem / "~pc_relatorios.json"
        tmp.write_text(json.dumps({"quando": datetime.now().strftime("%Y-%m-%d %H:%M"), "relatorios": rel}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(nuvem / "pc_relatorios.json")
        estado["relatorios_hash"] = h_rel
    # Sinal de vida para o painel «Nuvem» da app (de 30 em 30 minutos, para não encher o Google Drive de mudanças)
    if time.time() - estado.get("pc_visto", 0) > 1800:
        from datetime import datetime
        (nuvem / "pc_visto.json").write_text(json.dumps({"quando": datetime.now().strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")
        estado["pc_visto"] = time.time()


# Do PC para a nuvem (desde 20/10/2026): os dados de trabalho das apps do PC vão para a pasta «Centro - nuvem» do Google
# Drive, no mesmo formato que o trabalhador da nuvem lê (pacotes de trabalho.py). A app na internet é só para ver.
_CASA = Path.home()
PACOTES_PC = {
    "dados_decisao.json": [(_CASA / "OneDrive" / "Documentos" / "Decisão de Investimento" / "Dados da app", "", "*")],
    "dados_centro.json": [(_CASA / "Documents" / "centro_investimento" / "dados", "", "*"), (ESTUDOS / "_PEDIDOS", "pedidos_cowork/", "*.json")],
    "dados_descoberta.json": [(ESTUDOS / "APP" / "triagem_acoes" / "pesquisas", "pesquisas/", "*"),
                              (ESTUDOS / "APP" / "triagem_acoes" / "resultados", "resultados/", "*")],
}
MIN_ENTRE_ENVIOS = 60  # segundos: várias gravações seguidas (ex.: uma análise) vão num só envio


def _ficheiros_pc(nome: str) -> list[tuple[str, Path]]:
    out = []
    for pasta, prefixo, padrao in PACOTES_PC[nome]:
        if pasta.exists():
            for f in sorted(pasta.rglob(padrao)):
                if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith((".tmp", ".lock")):
                    out.append((prefixo + f.relative_to(pasta).as_posix(), f))
    return out


def para_nuvem(pasta: Path | None, estado: dict) -> None:
    import base64
    import gzip
    import hashlib

    nuvem = pasta.parent / "Centro - nuvem" if pasta else None
    if not nuvem or not nuvem.is_dir() or time.time() - estado.get("pacotes_envio", 0) < MIN_ENTRE_ENVIOS:
        return
    assinaturas = estado.setdefault("pacotes_assinatura", {})
    for nome in PACOTES_PC:
        fic = _ficheiros_pc(nome)
        # assinatura barata (nomes, tamanhos e datas): só se empacota quando algo mudou
        assin = hashlib.sha1(json.dumps([(r, f.stat().st_size, int(f.stat().st_mtime)) for r, f in fic]).encode("utf-8")).hexdigest()
        if assinaturas.get(nome) == assin:
            continue
        pacote = {"versao": 1, "ficheiros": {r: base64.b64encode(gzip.compress(f.read_bytes(), 6, mtime=0)).decode("ascii") for r, f in fic},
                  "datas": {r: int(f.stat().st_mtime) for r, f in fic}}
        tmp = nuvem / f"~{nome}"
        tmp.write_text(json.dumps(pacote, sort_keys=True), encoding="utf-8")
        tmp.replace(nuvem / nome)
        assinaturas[nome] = assin
        estado["pacotes_envio"] = time.time()
        registar(f"Para a nuvem (só para ver): {nome} ({len(fic)} ficheiros).")


def uma_volta(pasta: Path | None, forcar: bool = False) -> None:
    estado = ler_estado()
    if not NA_NUVEM:
        try:
            if not SO_PONTE_COWORK:
                para_nuvem(pasta, estado)
            da_nuvem(pasta, estado)
        except Exception as e:  # noqa: BLE001  (a nuvem é um extra: não pode parar a sincronização)
            registar(f"Da nuvem: {type(e).__name__}: {e}")
        ESTADO.write_text(json.dumps(estado), encoding="utf-8")
        if SO_PONTE_COWORK:
            return
    pedidos, carteira = ler_pedidos(pasta)
    novos = [p for p in pedidos if p["id"] not in estado["tratados"]]
    triagens = [p for p in novos if p.get("acao") in ("triagem", "pares")]
    listas = [p for p in novos if p.get("acao") not in ("triagem", "pares")]
    if triagens and pasta:
        tratar_triagens(triagens, estado, pasta)
    if pasta and time.time() - estado.get("ultima_tecnica", 0) > MINUTOS_TECNICA * 60:
        try:
            atualizar_tecnica(pasta, carteira)
        except Exception as e:  # noqa: BLE001
            registar(f"Técnica rápida falhou: {type(e).__name__}: {e}")
        estado["ultima_tecnica"] = time.time()
        ESTADO.write_text(json.dumps(estado), encoding="utf-8")
    atrasada = time.time() - estado.get("ultima_completa", 0) > HORAS_ENTRE_COMPLETAS * 3600
    mudaram = listas_mudaram(estado)
    forcar = forcar or bool(estado.pop("forcar_completa", False))
    if not (forcar or listas or atrasada or mudaram):
        return
    if mudaram and not listas:
        registar("As listas da Decisão de Investimento mudaram: a sincronizar.")
    try:
        tratados = tratar_pedidos(listas, estado) if listas else []
        saida = sincronizar(carteira, tratados, so_relatorios="--so-relatorios" in sys.argv)
        gravar(saida, pasta)
        if "decisao" in saida:
            estado["ultima_completa"] = time.time()
            try:
                import hashlib
                from config import pasta_dados  # noqa: E402
                estado["listas_hash"] = hashlib.sha1((pasta_dados() / "listas.json").read_bytes()).hexdigest()
            except (ImportError, OSError):
                pass
    finally:
        ESTADO.write_text(json.dumps(estado), encoding="utf-8")


def vigilante_ativo() -> bool:
    try:
        return time.time() - TRANCA.stat().st_mtime < 120
    except OSError:
        return False


def vigiar() -> None:
    """Fica a correr (sem janela): de 15 em 15 segundos vê se há pedidos da app ou mudanças nas listas."""
    if vigilante_ativo():
        return  # já há um vigilante a correr
    registar("Vigilante iniciado.")
    pasta = None
    while True:
        TRANCA.write_text(str(os.getpid()), encoding="utf-8")
        try:
            pasta = pasta or pasta_drive()
            uma_volta(pasta)
        except Exception as e:  # noqa: BLE001  (um erro numa volta não pode parar o vigilante)
            registar(f"Erro no vigilante: {type(e).__name__}: {e}")
        time.sleep(15)


def main() -> None:
    if NA_NUVEM:  # uma volta e sai (o trabalhador da nuvem chama-a em cada execução); --tudo força a sincronização completa
        uma_volta(pasta_drive(), forcar="--tudo" in sys.argv)
        return
    if "--vigiar" in sys.argv:
        vigiar()
        return
    if "--auto" in sys.argv:
        if vigilante_ativo():
            return  # o vigilante já está a tratar de tudo
        # Rede de segurança: o vigilante não está a correr. Faz esta volta e volta a pô-lo de pé.
        import subprocess
        pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(Path(__file__).resolve()), "--vigiar"],
                         creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return
    uma_volta(pasta_drive(), forcar=True)


if __name__ == "__main__":
    main()
