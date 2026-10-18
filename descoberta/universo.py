"""
Descoberta: listar todas as empresas cotadas dos EUA, do Canadá e da Europa que passam
um primeiro filtro barato (Yahoo screener) e analisá-las a fundo.

Duas fases, porque a análise completa custa ~9 pedidos ao Yahoo por empresa:
1. Screener: capitalização mínima e, opcionalmente, ROIC mínimo e EV/EBIT máximo.
   Um só pedido por cada 250 empresas.
2. Análise completa (motor.analisar_ticker) só das que passam, em paralelo.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from avaliacao import Parametros
from motor import analisar_ticker, linha_resumo

PASTA_RESULTADOS = Path(__file__).parent / "resultados"

# região do Yahoo → (nome, bolsas principais, moeda da capitalização no screener)
REGIOES = {
    "us": ("EUA", ["NMS", "NYQ", "NGM", "NCM", "ASE"], "USD"),
    "ca": ("Canadá", ["TOR"], "CAD"),
    "gb": ("Reino Unido", ["LSE"], "GBP"),
    "de": ("Alemanha", ["GER"], "EUR"),
    "fr": ("França", ["PAR"], "EUR"),
    "nl": ("Países Baixos", ["AMS"], "EUR"),
    "es": ("Espanha", ["MCE"], "EUR"),
    "it": ("Itália", ["MIL"], "EUR"),
    "ch": ("Suíça", ["EBS"], "CHF"),
    "se": ("Suécia", ["STO"], "SEK"),
    "dk": ("Dinamarca", ["CPH"], "DKK"),
    "no": ("Noruega", ["OSL"], "NOK"),
    "fi": ("Finlândia", ["HEL"], "EUR"),
    "be": ("Bélgica", ["BRU"], "EUR"),
    "pt": ("Portugal", ["LIS"], "EUR"),
    "at": ("Áustria", ["VIE"], "EUR"),
    "ie": ("Irlanda", ["ISE"], "EUR"),
}
REGIOES_EUROPA = [r for r in REGIOES if r not in ("us", "ca")]
# O screener compara a capitalização de Londres em pence (GBp), embora a devolva em libras: sem este fator,
# o limite mínimo no Reino Unido ficava 100 vezes mais baixo e entravam empresas britânicas pequenas demais.
FATOR_LIMITE_SCREENER = {"gb": 100}

# países de sede aceites (nomes como o Yahoo os devolve)
PAISES_EUROPA = {
    "United Kingdom", "Germany", "France", "Netherlands", "Spain", "Italy", "Switzerland",
    "Sweden", "Denmark", "Norway", "Finland", "Belgium", "Portugal", "Austria", "Ireland", "Luxembourg",
    "Jersey", "Guernsey", "Isle of Man", "Iceland", "Poland", "Greece", "Czech Republic",
}
PAISES_ACEITES = {"United States", "Canada"} | PAISES_EUROPA


def paises_das_regioes(regioes: list[str]) -> set[str]:
    """Sedes aceites para as regiões escolhidas: só o Canadá não deixa entrar americanas cotadas em Toronto."""
    out = set()
    if "us" in regioes:
        out.add("United States")
    if "ca" in regioes:
        out.add("Canada")
    if any(r in REGIOES_EUROPA for r in regioes):
        out |= PAISES_EUROPA
    return out

# bolsas europeias onde é normal haver empresas locais a reportar em USD (Shell, HSBC, Novartis, Equinor...)
BOLSAS_USD_LOCAIS = {"LSE", "EBS", "AMS", "CPH", "OSL", "STO"}
MOEDAS_EUROPA = {"EUR", "GBP", "CHF", "SEK", "DKK", "NOK", "ISK", "PLN", "CZK"}
PRIORIDADE_BOLSA = ["NYQ", "NMS", "TOR", "LSE", "PAR", "AMS", "GER", "EBS", "MIL", "MCE", "STO", "CPH", "OSL",
                    "HEL", "BRU", "LIS", "VIE", "ISE", "NGM", "NCM", "ASE"]

_SUFIXOS_NOME = {"inc", "corp", "corporation", "co", "company", "plc", "sa", "se", "nv", "ag", "ab", "asa", "as",
                 "spa", "ltd", "limited", "holding", "holdings", "group", "the", "class", "a", "b", "r", "reg",
                 "ord", "shs", "sab", "oyj", "publ", "de", "cv"}


def _nome_normalizado(nome: str) -> str:
    # O Yahoo às vezes não tem o nome (vem NaN): sem esta guarda a pesquisa inteira falhava (EUROPA, 05/10/2026)
    palavras = re.sub(r"[^a-z0-9 ]", " ", (nome if isinstance(nome, str) else "").lower()).split()
    return " ".join(p for p in palavras if p not in _SUFIXOS_NOME)


def _cambio_para(moeda: str) -> float:
    """Unidades de `moeda` por 1 USD (para converter o limite de capitalização)."""
    if moeda == "USD":
        return 1.0
    from dados import serie_cambio
    s = serie_cambio("USD", moeda)
    if s is None or len(s) == 0:
        raise ValueError(f"Sem taxa de câmbio USD→{moeda}")
    return float(s.iloc[-1])


def listar_universo(regioes: list[str], mcap_min_usd: float = 2e9, condicoes: list[tuple] | None = None,
                    setores: list[str] | None = None, progresso=None, industrias: list[str] | None = None) -> pd.DataFrame:
    """condicoes = [(campo do Yahoo, 'gt' | 'lt' | 'btwn', valor)]; 'btwn' significa entre 0 e o valor.
    setores = nomes do Yahoo (ver filtros.SETORES); vazio = todos. industrias = nomes do screener do Yahoo
    (yfinance.const.EQUITY_SCREENER_EQ_MAP["industry"], ex.: «Software—Infrastructure»); vazio = todas."""
    import yfinance as yf
    from yfinance import EquityQuery as Q

    from dados import com_repeticao

    cotacoes = []
    for i, reg in enumerate(regioes):
        nome, bolsas, moeda = REGIOES[reg]
        if progresso:
            progresso(i / len(regioes), f"A listar {nome}...")
        cambio = _cambio_para(moeda)
        filtros = [Q("eq", ["region", reg]), Q("is-in", ["exchange", *bolsas]),
                   Q("gt", ["intradaymarketcap", mcap_min_usd * cambio * FATOR_LIMITE_SCREENER.get(reg, 1)])]
        if setores:
            filtros.append(Q("is-in", ["sector", *setores]) if len(setores) > 1 else Q("eq", ["sector", setores[0]]))
        if industrias:
            filtros.append(Q("is-in", ["industry", *industrias]) if len(industrias) > 1 else Q("eq", ["industry", industrias[0]]))
        for campo, operador, valor in condicoes or []:
            filtros.append(Q("btwn", [campo, 0, valor]) if operador == "btwn" else Q(operador, [campo, valor]))
        consulta = Q("and", filtros)
        offset = 0
        while True:
            r = com_repeticao(lambda: yf.screen(consulta, size=250, offset=offset,
                                                sortField="intradaymarketcap", sortAsc=False))
            quotes = (r or {}).get("quotes", [])
            for x in quotes:
                cotacoes.append({
                    "Ticker": x.get("symbol"), "Empresa": x.get("longName") or x.get("shortName"),
                    "Nome curto": x.get("shortName") or "",
                    "Bolsa": x.get("exchange"), "Região": nome, "Moeda": x.get("currency"),
                    "Moeda das contas": x.get("financialCurrency"), "Capitalização": x.get("marketCap"),
                    "Capitalização (USD)": (x.get("marketCap") or 0) / cambio,
                    # valor negociado por dia, em USD: a bolsa de origem é quase sempre onde mais se negoceia
                    "Negociação (USD)": ((x.get("averageDailyVolume3Month") or 0) * (x.get("regularMarketPrice") or 0)
                                         * (0.01 if x.get("currency") in ("GBp", "GBX") else 1) / cambio),
                    "P/E previsto": x.get("forwardPE"), "Variação 12 meses": x.get("fiftyTwoWeekChangePercent"),
                    "Tipo": x.get("quoteType"),
                })
            offset += len(quotes)
            if not quotes or offset >= (r or {}).get("total", 0):
                break
            time.sleep(0.5)
    if progresso:
        progresso(1.0, "Lista concluída")

    df = pd.DataFrame(cotacoes)
    if df.empty:
        return df
    df = df[df["Tipo"] == "EQUITY"].drop_duplicates("Ticker")
    # ações preferenciais (JPM-PC, BCE-PL.TO, TD-PFI.TO): têm a capitalização da empresa e passariam por ela
    df = df[~df["Ticker"].str.contains(r"-P[A-Z0-9]*(?:\.[A-Z]+)?$", regex=True)]
    # códigos 0XXX.L de Londres: cópias de ações estrangeiras no livro internacional, não empresas britânicas
    df = df[~df["Ticker"].str.match(r"^0[A-Z0-9]{3}\.L$")]
    # CDR de Toronto (NVDA.TO, AAPL.TO): certificados de empresas americanas, não empresas canadianas
    df = df[~((df["Bolsa"] == "TOR") & df["Nome curto"].str.contains(r"\bCDR\b", regex=True))]
    # ADR nos EUA de empresas que reportam fora da América do Norte e da Europa (Takeda, Alibaba, TSMC...)
    moedas_aceites = MOEDAS_EUROPA | {"USD", "CAD"}
    eua = df["Bolsa"].isin(REGIOES["us"][1])
    df = df[~eua | df["Moeda das contas"].isin(moedas_aceites) | df["Moeda das contas"].isna()]
    # empresas estrangeiras cotadas em bolsas europeias (ex.: NVIDIA em Frankfurt): fora
    europa = df["Bolsa"].isin({b for r in REGIOES_EUROPA for b in REGIOES[r][1]})
    moeda_contas_local = df["Moeda das contas"].isin(MOEDAS_EUROPA) | (
        (df["Moeda das contas"] == "USD") & df["Bolsa"].isin(BOLSAS_USD_LOCAIS)
    ) | df["Moeda das contas"].isna()
    df = df[~europa | moeda_contas_local]
    # a mesma empresa em várias bolsas ou classes de ações: fica uma listagem
    df["_nome"] = df["Empresa"].map(_nome_normalizado)
    df["_mesma_moeda"] = (df["Moeda"].str.upper().str[:3] == df["Moeda das contas"]).astype(int)
    df["_prio"] = df["Bolsa"].map(lambda b: PRIORIDADE_BOLSA.index(b) if b in PRIORIDADE_BOLSA else 99)
    # entre listagens da mesma empresa: a que cota na moeda das contas e, dentro dessas, a mais negociada
    # (Santander: Madrid e não Frankfurt; ASML: Amesterdão e não o ADR de Nova Iorque)
    df = df.sort_values(["_nome", "_mesma_moeda", "Negociação (USD)", "_prio"], ascending=[True, False, False, True])
    df = df.drop_duplicates("_nome").drop(columns=["_nome", "_mesma_moeda", "_prio", "Tipo", "Nome curto"])
    return df.sort_values("Capitalização (USD)", ascending=False).reset_index(drop=True)


# Ordem pela qual o universo é analisado quando passa do limite de empresas a analisar.
ORDENS = [
    "Maiores primeiro",
    "P/E previsto mais baixo primeiro",
    "As que mais caíram em 12 meses primeiro",
    "Mais pequenas primeiro",
    "Ordem aleatória (uma amostra diferente de cada vez)",
]


def ordenar(uni: pd.DataFrame, ordem: str) -> pd.DataFrame:
    if uni.empty:
        return uni
    if ordem.startswith("P/E"):
        pe = uni["P/E previsto"].where(uni["P/E previsto"] > 0)  # prejuízo previsto vai para o fim
        return uni.assign(_o=pe).sort_values("_o", na_position="last").drop(columns="_o").reset_index(drop=True)
    if ordem.startswith("As que mais caíram"):
        return uni.sort_values("Variação 12 meses", na_position="last").reset_index(drop=True)
    if ordem.startswith("Mais pequenas"):
        return uni.sort_values("Capitalização (USD)").reset_index(drop=True)
    if ordem.startswith("Ordem aleatória"):
        return uni.sample(frac=1).reset_index(drop=True)
    return uni.sort_values("Capitalização (USD)", ascending=False).reset_index(drop=True)


def analisar_lote(tickers: list[str], p: Parametros | None = None, filtrar_paises: bool = True,
                  progresso=None, trabalhadores: int = 3,
                  paises: set[str] | None = None) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Análise completa de uma lista de tickers. Devolve (tabela resumo, falhas)."""
    linhas, falhas = [], []
    feitos = 0

    def registar(t, a):
        if filtrar_paises and a["res"].get("pais") and a["res"]["pais"] not in (paises or PAISES_ACEITES):
            falhas.append((t, f"sede fora das regiões escolhidas ({a['res']['pais']})"))
        else:
            linhas.append(linha_resumo(a))

    with ThreadPoolExecutor(max_workers=trabalhadores) as ex:
        futuros = {ex.submit(analisar_ticker, t, p): t for t in tickers}
        for fut in as_completed(futuros):
            t = futuros[fut]
            feitos += 1
            try:
                registar(t, fut.result())
            except Exception as e:  # noqa: BLE001
                falhas.append((t, str(e)[:200]))
            if progresso:
                progresso(feitos / len(tickers), f"{t} ({feitos}/{len(tickers)})")

    # Quando se pede muito depressa, o Yahoo devolve dados vazios em vez de um erro: a empresa parecia não ter
    # dados. Segunda tentativa, uma a uma e com pausas, para as que falharam por isso.
    repetir = [t for t, m in falhas if not m.startswith("sede")]
    if repetir:
        falhas = [(t, m) for t, m in falhas if m.startswith("sede")]
        time.sleep(5)
        for i, t in enumerate(repetir, start=1):
            if progresso:
                progresso(i / len(repetir), f"Segunda tentativa: {t} ({i}/{len(repetir)})")
            try:
                registar(t, analisar_ticker(t, p))
            except Exception as e:  # noqa: BLE001
                falhas.append((t, str(e)[:200]))
            time.sleep(1.0)
    df = pd.DataFrame(linhas)
    if not df.empty:
        df = df.sort_values(["ordem", "Preço (nota)", "Qualidade"], ascending=[True, False, False]).reset_index(drop=True)
    return df, falhas


def gravar_resultados(df: pd.DataFrame, descricao: str) -> Path:
    PASTA_RESULTADOS.mkdir(exist_ok=True)
    base = PASTA_RESULTADOS / f"descoberta_{datetime.now():%Y-%m-%d_%H%M}"
    df.attrs["descricao"] = descricao
    df.to_pickle(base.with_suffix(".pkl"))
    df.drop(columns=["ordem"], errors="ignore").to_excel(base.with_suffix(".xlsx"), index=False)
    return base.with_suffix(".xlsx")


def resultados_gravados() -> list[Path]:
    if not PASTA_RESULTADOS.exists():
        return []
    return sorted(PASTA_RESULTADOS.glob("descoberta_*.pkl"), reverse=True)
