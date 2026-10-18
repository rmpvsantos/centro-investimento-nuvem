"""
Diário de decisões (pedido do Rui a 18/10/2026): mede se o método funciona, com factos.

Duas fontes, nenhuma inventada:
  1. Recomendações dos teus relatórios: cada relatório da wiki com uma recomendação reconhecida (COMPRAR, ACUMULAR,
     MANTER, REDUZIR, VENDER; NEUTRO e AGUARDAR = MANTER, EVITAR = VENDER), na data do relatório. Vêm da wiki, por isso
     o histórico começa nos relatórios mais antigos (março de 2026).
  2. Sinais da app: cada vez que o sinal de uma empresa da carteira ou da observação muda (e a primeira vez que é
     calculado), fica registado em diario.json, com a data, o preço e o motivo. Começa a 18/10/2026.

Desempenho de cada entrada, em euros e com dividendos (fechos ajustados do Yahoo, convertidos ao câmbio do Yahoo do
mesmo dia), contra o índice de referência da Carteira (MSCI World, IWDA.AS, em euros e de acumulação):
  - «em vigor»: da data da entrada até à entrada seguinte da mesma empresa e da mesma fonte (ou até hoje);
  - aos 6 e aos 12 meses da data da entrada, quando já passaram.
Excesso = retorno da ação − retorno do índice. A favor da decisão: COMPRAR / ACUMULAR com excesso positivo; REDUZIR /
VENDER com excesso negativo; MANTER não conta como acerto nem como erro (fica só a informação).
Funções puras (sem rede): testadas em testes/teste_diario.py.
"""
from __future__ import annotations

import bisect
import json
from datetime import date, timedelta
from pathlib import Path

DIRECAO = {"comprar": 1, "acumular": 1, "manter": 0, "reduzir": -1, "vender": -1}
GRUPO = {1: "compra", 0: "manter", -1: "venda"}
HORIZONTES = (("6m", 182), ("12m", 365))
BENCHMARK_OMISSAO = "IWDA.AS"
TIPOS_RELATORIO = ("iniciacao", "trimestral", "acompanhamento", "fundamental")


# ---------------------------------------------------------------------------
# Ficheiro (sinais da app)
# ---------------------------------------------------------------------------
def ler(ficheiro: Path) -> dict:
    try:
        d = json.loads(ficheiro.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"sinais": []}
    except (OSError, ValueError):
        return {"sinais": []}


def gravar(ficheiro: Path, diario: dict) -> None:
    ficheiro.parent.mkdir(parents=True, exist_ok=True)
    tmp = ficheiro.with_suffix(".tmp")
    tmp.write_text(json.dumps(diario, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ficheiro)


def registar_sinal(diario: dict, ticker: str, nivel: str, rotulo: str, agora: str, **extra) -> dict | None:
    """Acrescenta uma entrada quando o sinal (o nível) muda ou na primeira vez. agora: «AAAA-MM-DD HH:MM».
    extra: nome, lista, preco, moeda, origem, motivo. Devolve a entrada nova (ou None se nada mudou)."""
    if nivel not in DIRECAO:
        return None
    sinais = diario.setdefault("sinais", [])
    ultimo = next((e for e in reversed(sinais) if e["ticker"] == ticker), None)
    if ultimo and ultimo["para_nivel"] == nivel:
        return None
    e = {"ticker": ticker, "quando": agora, "data": agora[:10], "para": rotulo, "para_nivel": nivel,
         "de": ultimo["para"] if ultimo else None, "de_nivel": ultimo["para_nivel"] if ultimo else None,
         "inicio": ultimo is None, **{k: v for k, v in extra.items() if v is not None}}
    sinais.append(e)
    return e


# ---------------------------------------------------------------------------
# Recomendações dos relatórios (da wiki)
# ---------------------------------------------------------------------------
def entradas_relatorios(empresas: list[dict], relatorios_de, ordem, nivel_de) -> list[dict]:
    """empresas: as do índice da wiki; relatorios_de(empresa) -> relatórios; ordem: chave de ordenação; nivel_de(texto) ->
    COMPRAR | ACUMULAR | MANTER | REDUZIR | VENDER | None."""
    saida = []
    for emp in empresas:
        if emp.get("categoria") == "duplicado" or not emp.get("ticker_yahoo"):
            continue
        rels = sorted((r for r in relatorios_de(emp) if not r.get("arquivado") and r.get("tipo") in TIPOS_RELATORIO), key=ordem)
        for r in rels:
            nivel = nivel_de(r.get("recomendacao"))
            if not nivel:
                continue
            saida.append({"ticker": emp["ticker_yahoo"].upper(), "nome": emp.get("nome"), "categoria": emp.get("categoria"),
                          "data": r["data"], "para": r["recomendacao"], "para_nivel": nivel.lower(), "relatorio": r["id"],
                          "titulo": r.get("titulo"), "tipo_relatorio": r.get("tipo")})
    return saida


# ---------------------------------------------------------------------------
# Desempenho
# ---------------------------------------------------------------------------
def valor_em(serie: list | None, dia: str) -> tuple[str, float] | None:
    """Último fecho com data <= dia (serie = [(«AAAA-MM-DD», valor), …] por ordem). None antes do primeiro."""
    if not serie:
        return None
    i = bisect.bisect_right([d for d, _ in serie], dia) - 1
    return serie[i] if i >= 0 else None


def retorno(serie, fx, bench, d0: str, d1: str | None) -> dict | None:
    """Retorno em euros entre d0 e d1 (None = último fecho), o do índice e o excesso. fx None = a ação já cota em euros."""
    fim = d1 or (serie[-1][0] if serie else None)
    if not fim:
        return None
    a0, a1 = valor_em(serie, d0), valor_em(serie, fim)
    b0, b1 = valor_em(bench, d0), valor_em(bench, fim)
    if not (a0 and a1 and b0 and b1) or a0[0] == a1[0]:
        return None
    c0 = (1.0, 1.0) if fx is None else (valor_em(fx, d0), valor_em(fx, fim))
    if fx is not None:
        if not (c0[0] and c0[1]):
            return None
        c0 = (c0[0][1], c0[1][1])
    r = a1[1] * c0[1] / (a0[1] * c0[0]) - 1
    rb = b1[1] / b0[1] - 1
    return {"de": a0[0], "ate": a1[0], "retorno": r, "indice": rb, "excesso": r - rb}


def desempenho(entradas: list[dict], series: dict, fx: dict, bench: list, hoje: str) -> list[dict]:
    """entradas: com «fonte» («relatorio» ou «sinal»), «ticker», «data», «para_nivel». series[ticker] = {"serie", "moeda"};
    fx[moeda] = serie em euros (EUR fica None). Devolve as entradas com «em_vigor», «6m», «12m», «a_favor» e «fim»."""
    por_grupo: dict[tuple, list[dict]] = {}
    for e in sorted(entradas, key=lambda x: (x["fonte"], x["ticker"], x["data"])):
        por_grupo.setdefault((e["fonte"], e["ticker"]), []).append(e)
    saida = []
    for lista in por_grupo.values():
        for i, e in enumerate(lista):
            s = series.get(e["ticker"]) or {}
            serie, moeda = s.get("serie"), s.get("moeda")
            seguinte = next((x for x in lista[i + 1:] if x["data"] > e["data"]), None)
            fim = seguinte["data"] if seguinte else None
            out = {**e, "fim": fim, "moeda_serie": moeda, "em_vigor": None, "6m": None, "12m": None, "motivo": None}
            if not serie:
                out["motivo"] = s.get("erro") or "sem preços no Yahoo"
            elif moeda and moeda != "EUR" and not fx.get(moeda):
                out["motivo"] = f"sem câmbio {moeda}/EUR no Yahoo"
            else:
                cambio = None if (moeda or "EUR") == "EUR" else fx[moeda]
                out["em_vigor"] = retorno(serie, cambio, bench, e["data"], fim)
                ultimo = serie[-1][0]
                for nome, dias in HORIZONTES:
                    alvo = (date.fromisoformat(e["data"]) + timedelta(days=dias)).isoformat()
                    if alvo <= ultimo:
                        out[nome] = retorno(serie, cambio, bench, e["data"], alvo)
                if out["em_vigor"] is None:
                    out["motivo"] = "ainda sem fecho depois da data da entrada" if e["data"] >= ultimo else "sem preço nessa data"
            d = DIRECAO.get(e["para_nivel"], 0)
            out["direcao"] = d
            for k in ("em_vigor", "6m", "12m"):
                r = out[k]
                if r is not None:
                    r["a_favor"] = None if d == 0 else (r["excesso"] * d > 0)
            saida.append(out)
    return sorted(saida, key=lambda x: (x["data"], x["ticker"]), reverse=True)


def resumir(entradas_medidas: list[dict]) -> list[dict]:
    """Por fonte e grupo de decisão (compra, manter, venda): quantas, quantas medidas, acertos e excesso médio, para o
    período em vigor e aos 6 e aos 12 meses."""
    saida = []
    for fonte in ("relatorio", "sinal"):
        for d, grupo in GRUPO.items():
            es = [e for e in entradas_medidas if e["fonte"] == fonte and e["direcao"] == d]
            if not es:
                continue
            linha = {"fonte": fonte, "grupo": grupo, "n": len(es)}
            for k in ("em_vigor", "6m", "12m"):
                med = [e[k] for e in es if e.get(k)]
                linha[k] = {"n": len(med),
                            "excesso_medio": sum(x["excesso"] for x in med) / len(med) if med else None,
                            "retorno_medio": sum(x["retorno"] for x in med) / len(med) if med else None,
                            "acertos": sum(1 for x in med if x.get("a_favor")) if d else None}
            saida.append(linha)
    return saida
