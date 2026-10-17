"""Obtenção de dados do Yahoo Finance (gratuito, sem chave) através do yfinance."""

from __future__ import annotations

import re


def obter_dados(ticker: str) -> dict:
    import yfinance as yf  # importado aqui para o motor de cálculo poder ser testado sem rede

    ticker = ticker.strip().upper()
    t = yf.Ticker(ticker)

    try:
        info = t.info or {}
    except Exception:
        info = {}

    income = t.income_stmt
    if income is None or income.empty:
        raise ValueError(
            f"Sem dados para '{ticker}'. Confirma o ticker (europeus levam sufixo da bolsa: "
            "ASML.AS, SAP.DE, MC.PA, NOVO-B.CO, SHEL.L) ou tenta novamente dentro de alguns minutos "
            "(o Yahoo limita pedidos em excesso)."
        )

    precos = t.history(period="7y", auto_adjust=False)
    precos = precos["Close"] if precos is not None and not precos.empty else None

    try:
        splits = t.splits
    except Exception:
        splits = None

    dados = {
        "ticker": ticker,
        "info": info,
        "income": income,
        "balance": t.balance_sheet,
        "cashflow": t.cashflow,
        "income_q": t.quarterly_income_stmt,
        "cashflow_q": t.quarterly_cashflow,
        "precos": precos,
        "splits": splits,
    }
    destino, origem = moeda_destino(info), info.get("financialCurrency")
    if destino and origem and destino != origem:
        de, para = obter_taxa_cambio(origem), obter_taxa_cambio(destino)
        if de and para:
            converter_moeda(dados, de / para, origem, destino)
    return dados


# Campos do Yahoo (info) que vêm na moeda das demonstrações financeiras, não na da cotação
CAMPOS_INFO_FINANCEIROS = ("totalRevenue", "ebitda", "freeCashflow", "operatingCashflow", "totalCash", "totalDebt",
                           "grossProfits", "netIncomeToCommon", "revenuePerShare", "totalCashPerShare", "bookValue")


def moeda_destino(info: dict) -> str | None:
    """Moeda em que a ação cota (pence passam a libras)."""
    m = info.get("currency")
    return "GBP" if m == "GBp" else m


def converter_moeda(dados: dict, taxa: float, origem: str, destino: str) -> dict:
    """
    Converte as demonstrações financeiras para a moeda da cotação (ex.: CNI cota em USD e reporta em CAD; ABB.ST cota em
    SEK e reporta em USD). Usa a taxa de hoje para todos os anos: os rácios e o crescimento não mudam, os valores
    históricos em moeda ficam aproximados. Sem isto, a app recusava calcular o valor destas empresas.
    """
    for k in ("income", "balance", "cashflow", "income_q", "cashflow_q"):
        df = dados.get(k)
        if df is not None and not df.empty:
            df = df.copy()
            monetarias = [r for r in df.index if not re.search(r"(?i)shares|tax rate", str(r))]  # n.º de ações e taxas não
            df.loc[monetarias] = df.loc[monetarias] * taxa
            dados[k] = df
    info = dados["info"]
    for k in CAMPOS_INFO_FINANCEIROS:
        if isinstance(info.get(k), (int, float)):
            info[k] = info[k] * taxa
    info["financialCurrency"] = destino
    info["conversao_moeda"] = {"de": origem, "para": destino, "taxa": taxa}
    return dados


def obter_historico(ticker: str, periodo: str = "5y") -> dict:
    """Preços diários (abertura, máximo, mínimo, fecho, volume) para a análise técnica. Cotações em pence (GBp) passam a libras."""
    import yfinance as yf

    t = yf.Ticker(ticker.strip().upper())
    h = t.history(period=periodo, auto_adjust=False)
    if h is None or h.empty:
        raise ValueError(f"Sem histórico de preços para '{ticker}'. Confirma o ticker (europeus levam sufixo da bolsa).")
    h = h[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    try:
        moeda = t.fast_info["currency"]
    except Exception:  # noqa: BLE001
        moeda = None
    if moeda == "GBp":
        h[["Open", "High", "Low", "Close"]] = h[["Open", "High", "Low", "Close"]] / 100
        moeda = "GBP"
    h.index = h.index.tz_localize(None) if h.index.tz is not None else h.index
    return {"ticker": ticker.strip().upper(), "moeda": moeda, "precos": h}


def validar_ticker(ticker: str) -> dict:
    """Confirma no Yahoo se o ticker existe. Se não existir, sugere alternativas (primeiro as que só acrescentam o
    sufixo da bolsa, como IMB → IMB.L; depois outras ações parecidas)."""
    import yfinance as yf

    t = ticker.strip().upper()
    try:
        cotacoes = yf.Search(t, max_results=8, news_count=0).quotes or []
    except Exception:  # noqa: BLE001
        cotacoes = []
    acoes = [c for c in cotacoes if c.get("quoteType") in ("EQUITY", "ETF") and c.get("symbol")]
    for c in acoes:
        if c["symbol"].upper() == t:
            return {"ticker": t, "valido": True, "nome": c.get("shortname") or c.get("longname"), "sugestoes": []}
    try:  # alguns tickers válidos não aparecem na pesquisa: confirmar pelo histórico
        if not yf.Ticker(t).history(period="5d").empty:
            return {"ticker": t, "valido": True, "nome": None, "sugestoes": []}
    except Exception:  # noqa: BLE001
        pass
    acoes.sort(key=lambda c: 0 if c["symbol"].upper().startswith(t + ".") else 1)
    sugestoes = [{"ticker": c["symbol"], "nome": c.get("shortname") or c.get("longname"), "bolsa": c.get("exchange")} for c in acoes[:4]]
    return {"ticker": t, "valido": False, "nome": None, "sugestoes": sugestoes}


def procurar_tickers(q: str, n: int = 8) -> list[dict]:
    """Sugestões enquanto se escreve (como a pesquisa da Carteira): ações e ETF do Yahoo para um ticker ou nome."""
    import yfinance as yf

    q = (q or "").strip()
    if len(q) < 2:
        return []
    cotacoes = yf.Search(q, max_results=n + 4, news_count=0).quotes or []
    return [{"ticker": c["symbol"], "nome": c.get("longname") or c.get("shortname") or c["symbol"],
             "bolsa": c.get("exchDisp") or c.get("exchange") or "", "tipo": "ETF" if c.get("quoteType") == "ETF" else "Ação"}
            for c in cotacoes if c.get("quoteType") in ("EQUITY", "ETF") and c.get("symbol")][:n]


def cotacao_rapida(ticker: str) -> dict:
    """Último preço e variação do dia (gráfico diário do Yahoo, sem chave), para as sugestões da caixa do topo."""
    import json
    import urllib.parse
    import urllib.request

    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker.strip().upper()) + "?range=1d&interval=1d"  # com 1 dia, chartPreviousClose = fecho da sessão anterior
    pedido = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(pedido, timeout=8) as r:
        m = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]["meta"]
    preco, anterior = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
    return {"ticker": ticker.upper(), "preco": preco, "moeda": m.get("currency"),
            "variacao": (preco / anterior - 1) if preco and anterior else None}


def obter_candidatos(industria_key: str | None, setor_key: str | None, limite: int = 25) -> dict:
    """Empresas de referência da indústria e do setor, segundo o Yahoo (ordenadas por peso de mercado)."""
    import yfinance as yf

    def maiores(classe, chave):
        if not chave:
            return []
        try:
            tabela = classe(chave).top_companies
            return [str(s) for s in tabela.index][:limite] if tabela is not None else []
        except Exception:
            return []

    return {"industria": maiores(yf.Industry, industria_key), "setor": maiores(yf.Sector, setor_key)}


def obter_taxa_sem_risco() -> float | None:
    """Rendimento das obrigações do Tesouro dos EUA a 10 anos (^TNX no Yahoo), em fração (0,042 = 4,2%)."""
    import yfinance as yf

    try:
        h = yf.Ticker("^TNX").history(period="5d")["Close"].dropna()
        if h.empty:
            return None
        v = float(h.iloc[-1])
        if v > 25:  # formato antigo do Yahoo (rendimento x 10)
            v /= 10
        v /= 100
        return v if 0 < v < 0.15 else None
    except Exception:
        return None


def obter_taxa_cambio(moeda: str) -> float | None:
    """Quantos USD equivalem a uma unidade de `moeda`, segundo o Yahoo (ticker <MOEDA>USD=X). USD devolve sempre 1,0;
    moeda em branco ou desconhecida devolve None (não há como converter)."""
    m = (moeda or "").strip().upper()
    if not m:
        return None
    if m == "USD":
        return 1.0
    import yfinance as yf

    try:
        h = yf.Ticker(f"{m}USD=X").history(period="5d")["Close"].dropna()
        if h.empty:
            return None
        v = float(h.iloc[-1])
        return v if v > 0 else None
    except Exception:
        return None


def obter_cotacao(ticker: str) -> dict:
    """Última cotação e moeda, segundo o Yahoo. Cotações em pence (GBp) passam a libras (GBP). Sem dados: preco = None."""
    import yfinance as yf

    try:
        fi = yf.Ticker(ticker.strip().upper()).fast_info
        preco, moeda = fi["last_price"], fi["currency"]
        if preco is None or not (preco > 0):
            return {"preco": None, "moeda": moeda}
        if moeda == "GBp":
            preco, moeda = preco / 100, "GBP"
        return {"preco": float(preco), "moeda": moeda}
    except Exception:
        return {"preco": None, "moeda": None}


def obter_consenso_yahoo(ticker: str) -> dict:
    """Preços-alvo dos analistas segundo o Yahoo: {"media", "mediana", "minimo", "maximo", "n", "erro"} (valores None se não houver)."""
    import yfinance as yf

    vazio = {"media": None, "mediana": None, "minimo": None, "maximo": None, "n": None}
    try:
        t = yf.Ticker(ticker.strip().upper())
        alvos = t.analyst_price_targets
        if not alvos:
            return {**vazio, "erro": "o Yahoo não tem preços-alvo para esta empresa"}
        def num(k):
            v = alvos.get(k)
            return float(v) if v is not None and v == v and v > 0 else None
        n = None
        try:
            n = t.info.get("numberOfAnalystOpinions")
        except Exception:  # noqa: BLE001
            pass
        r = {"media": num("mean"), "mediana": num("median"), "minimo": num("low"), "maximo": num("high"),
             "n": int(n) if n else None, "erro": None}
        if r["media"] is None:
            r["erro"] = "o Yahoo não tem preços-alvo para esta empresa"
        return r
    except Exception as e:  # noqa: BLE001
        return {**vazio, "erro": f"o Yahoo não respondeu ({type(e).__name__})"}


def obter_html_stockanalysis(ticker: str) -> dict:
    """
    Página de previsões dos analistas do StockAnalysis (só empresas cotadas nos EUA, sem sufixo de bolsa). Um pedido por empresa,
    guardado em cache pela aplicação. Devolve {"html": texto ou None, "erro": motivo ou None}; nunca lança exceções.
    """
    import urllib.request

    simbolo = ticker.strip().lower().replace("-", ".")
    if not simbolo or ("." in simbolo and not simbolo.split(".")[-1] in ("a", "b", "c")):
        return {"html": None, "erro": "o StockAnalysis só está ligado para empresas cotadas nos EUA (sem sufixo de bolsa)"}
    url = f"https://stockanalysis.com/stocks/{simbolo}/forecast/"
    pedido = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(pedido, timeout=10) as r:
            return {"html": r.read().decode("utf-8", errors="replace"), "erro": None}
    except Exception as e:  # noqa: BLE001
        return {"html": None, "erro": f"o StockAnalysis não respondeu ({type(e).__name__}: {e})"}
