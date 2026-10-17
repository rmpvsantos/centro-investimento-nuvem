"""
Valor intrínseco de referência (sem Streamlit): DCF a 5 e a 10 anos ao WACC da empresa, valor por EV/EBITDA e por P/E
(mediana dos concorrentes), consenso dos analistas de várias fontes, mediana de todos os valores e três preços de entrada
com desconto (conservador, base e otimista). É uma referência de trabalho, não uma recomendação.
"""
from __future__ import annotations

import html as _html
import re
import statistics

import fundamental as F

NAN = float("nan")
ANOS_CURTO, ANOS_LONGO, ANOS_ATE_DESVANECER = 5, 10, 5
DESCONTOS = (("conservador", 0.20), ("base", 0.15), ("otimista", 0.10))
MINIMO_VALORES = 3          # menos do que isto não dá uma mediana com sentido
RAZAO_CONSENSO = (0.2, 5.0)  # um preço-alvo fora de 0,2x a 5x da cotação é tratado como erro de moeda ou de leitura


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf"))


def _n2(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def _pct(x: float, casas: int = 1) -> str:
    return f"{x * 100:.{casas}f}%".replace(".", ",")


# ---------------------------------------------------------------------------
# Valores calculados pela aplicação
# ---------------------------------------------------------------------------
def calcular_metodos(apoio: dict, mediana_ev_ebitda: float = NAN, mediana_pe: float = NAN,
                      manual_ev_ebitda: bool = False, manual_pe: bool = False) -> list[dict]:
    """
    Quatro valores por ação: DCF a 5 anos, DCF a 10 anos, EV/EBITDA e P/E. Cada linha: chave, rotulo, valor (None se não se
    conseguiu calcular), nota (o que foi usado, ou o motivo de não haver valor) e grupo "intrinseco".
    Em EV/EBITDA e P/E, o múltiplo por omissão é a mediana dos concorrentes; manual_ev_ebitda/manual_pe assinalam que o
    valor foi escrito à mão (muda só a nota, não o cálculo).
    """
    pdcf = F.pressupostos_dcf(apoio)
    wacc, g, gt = pdcf["taxa_desconto"], pdcf["crescimento"], pdcf["crescimento_terminal"]
    linhas = []

    def linha(chave, rotulo, valor, nota):
        linhas.append({"chave": chave, "rotulo": rotulo, "valor": valor, "nota": nota, "grupo": "intrinseco"})

    for anos, fade, chave in ((ANOS_CURTO, None, "dcf5"), (ANOS_LONGO, ANOS_ATE_DESVANECER, "dcf10")):
        rot = f"DCF a {anos} anos (ao WACC da empresa)"
        r = F.dcf(apoio, wacc, g, gt, anos, fade)
        if not r["ok"]:
            linha(chave, rot, None, r["motivo"])
            continue
        cresc = (f"crescimento do FCF de {_pct(g)} durante {anos} anos" if fade is None else
                 f"crescimento do FCF de {_pct(g)} nos primeiros {fade} anos, a descer até {_pct(gt)} no ano {anos}")
        linha(chave, rot, r["valor_acao"], f"WACC {_pct(wacc)}; {cresc}; crescimento perpétuo {_pct(gt)}; "
                                            f"valor terminal = {_pct(r['peso_terminal'], 0)} do valor da empresa")

    for tipo, med, chave, manual in (("ev_ebitda", mediana_ev_ebitda, "ev_ebitda", manual_ev_ebitda),
                                      ("pe", mediana_pe, "pe", manual_pe)):
        origem = "escrito à mão" if manual else "mediana dos concorrentes"
        rot = f"Valor por {F.TIPOS_MULTIPLO[tipo]} ({origem})"
        if not _ok(med) or med <= 0:
            linha(chave, rot, None, "sem múltiplo (nem mediana dos concorrentes, nem valor escrito à mão)")
            continue
        r = F.valor_por_multiplo(apoio, tipo, med)
        if not r["ok"]:
            linha(chave, rot, None, r["motivo"])
            continue
        base = "EBITDA dos últimos 12 meses" if tipo == "ev_ebitda" else "lucro por ação dos últimos 12 meses"
        linha(chave, rot, r["valor_acao"], f"múltiplo {origem} de {str(round(med, 1)).replace('.', ',')}x aplicado ao {base}")
    return linhas


# ---------------------------------------------------------------------------
# Consenso dos analistas
# ---------------------------------------------------------------------------
def interpretar_stockanalysis(html_bruto: str | None) -> dict | None:
    """Lê o preço-alvo médio (e mediana, mínimo, máximo, número de analistas) da página de previsões do StockAnalysis."""
    if not html_bruto:
        return None
    texto = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_bruto)
    texto = _html.unescape(re.sub(r"<[^>]+>", " ", texto))
    texto = re.sub(r"\s+", " ", texto)

    def num(s):
        return float(s.replace(",", ""))

    m = re.search(r"average price target of \$([\d,]+(?:\.\d+)?)", texto)
    if not m:
        return None
    r = {"media": num(m.group(1)), "mediana": None, "minimo": None, "maximo": None, "n": None, "erro": None}
    t = re.search(r"Price\s+\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)", texto)
    if t:
        r["minimo"], r["mediana"], r["maximo"] = num(t.group(1)), num(t.group(3)), num(t.group(4))
        if abs(num(t.group(2)) - r["media"]) > 0.02:  # a tabela e a frase têm de concordar; se não, fica só a frase
            r["minimo"] = r["mediana"] = r["maximo"] = None
    n = re.search(r"According to (\d+) analysts", texto)
    if n:
        r["n"] = int(n.group(1))
    return r


def linhas_consenso(yahoo: dict | None, stockanalysis: dict | None, erro_stockanalysis: str | None,
                    finviz: float | None, zacks: float | None, preco: float) -> list[dict]:
    """Uma linha por fonte de consenso (valor None e nota com o motivo quando não há valor utilizável)."""
    def valida(v):
        return _ok(v) and v > 0 and _ok(preco) and preco > 0 and RAZAO_CONSENSO[0] <= v / preco <= RAZAO_CONSENSO[1]

    def linha(chave, rotulo, valor, nota):
        if valor is not None and not valida(valor):
            nota, valor = f"descartado: {valor:.2f} é incompatível com a cotação (possível diferença de moeda ou de leitura)".replace(".", ","), None
        return {"chave": chave, "rotulo": rotulo, "valor": valor, "nota": nota, "grupo": "consenso"}

    out = []
    if yahoo and yahoo.get("media"):
        n = f"; {yahoo['n']} analistas" if yahoo.get("n") else ""
        med = f"; mediana {_n2(yahoo['mediana'])}" if yahoo.get("mediana") else ""
        out.append(linha("yahoo", "Consenso dos analistas: Yahoo (preço-alvo médio)", yahoo["media"], f"automático{n}{med}"))
    else:
        out.append(linha("yahoo", "Consenso dos analistas: Yahoo (preço-alvo médio)", None,
                         (yahoo or {}).get("erro") or "sem dados"))
    if stockanalysis and stockanalysis.get("media"):
        n = f"; {stockanalysis['n']} analistas" if stockanalysis.get("n") else ""
        med = f"; mediana {_n2(stockanalysis['mediana'])}" if stockanalysis.get("mediana") else ""
        out.append(linha("stockanalysis", "Consenso dos analistas: StockAnalysis (preço-alvo médio)", stockanalysis["media"], f"automático{n}{med}"))
    else:
        out.append(linha("stockanalysis", "Consenso dos analistas: StockAnalysis (preço-alvo médio)", None,
                         erro_stockanalysis or "não consegui ler a página do StockAnalysis"))
    out.append(linha("finviz", "Consenso dos analistas: Finviz (preço-alvo médio)", finviz,
                     "colado por ti" if finviz else "por preencher (o Finviz bloqueia leituras automáticas)"))
    out.append(linha("zacks", "Consenso dos analistas: Zacks (preço-alvo médio)", zacks,
                     "colado por ti" if zacks else "por preencher (o Zacks bloqueia leituras automáticas)"))
    return out


# ---------------------------------------------------------------------------
# Escolha dos métodos (por empresa, feita pelo Rui)
# ---------------------------------------------------------------------------
CHAVES_METODOS = ("dcf5", "dcf10", "ev_ebitda", "pe", "yahoo", "stockanalysis", "finviz", "zacks")


def aplicar_escolha(linhas: list[dict], excluidos) -> list[dict]:
    """Marca em cada linha se foi excluída por ti (`excluido`) e devolve só as que entram na mediana.
    Nada se apaga: a linha excluída continua visível, com o valor, mas não conta."""
    excl = {str(x) for x in (excluidos or [])}
    for l in linhas:
        l["excluido"] = l["chave"] in excl
    return [l for l in linhas if not l["excluido"]]


# ---------------------------------------------------------------------------
# Mediana e preços de entrada
# ---------------------------------------------------------------------------
def consolidar(linhas: list[dict], preco: float) -> dict:
    """
    Mediana de todos os valores disponíveis (métodos próprios e consenso) e os três preços de entrada com desconto:
    conservador (−20%), base (−15%) e otimista (−10%). Devolve também a mediana só com os métodos próprios.
    """
    valores = [l["valor"] for l in linhas if l["valor"] is not None]
    proprios = [l["valor"] for l in linhas if l["valor"] is not None and l["grupo"] == "intrinseco"]
    if len(valores) < MINIMO_VALORES:
        return {"ok": False, "n": len(valores),
                "motivo": f"só há {len(valores)} valor(es) utilizável(eis); são precisos pelo menos {MINIMO_VALORES} para uma mediana com sentido"}
    mediana = statistics.median(valores)
    entradas = {}
    for nome, d in DESCONTOS:
        v = mediana * (1 - d)
        entradas[nome] = {"desconto": d, "valor": v, "face": v / preco - 1 if _ok(preco) and preco > 0 else NAN}
    return {"ok": True, "motivo": None, "n": len(valores), "mediana": mediana,
            "face_mediana": mediana / preco - 1 if _ok(preco) and preco > 0 else NAN,
            "mediana_sem_consenso": statistics.median(proprios) if len(proprios) >= 2 else None, "n_proprios": len(proprios),
            "entradas": entradas, "preco": preco}
