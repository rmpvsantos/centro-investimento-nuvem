"""
Análise qualitativa por grelha com evidência (sem rede nem FastAPI: recebe os dados já obtidos).

GESTÃO: presidente, CEO e CFO (Yahoo) e uma grelha de 9 critérios, cada um de 0 a 2 pontos. Sete são pontuados
automaticamente a partir de factos (participação e transações dos insiders, riscos de governo da ISS, separação de
funções, alocação de capital); dois são teus (credibilidade e historial), com a evidência dos teus relatórios ao lado.
Qualquer nota automática pode ser substituída por ti; a substituição fica registada.

MOAT: força (indicador quantitativo de 0 a 5 e a nota do fosso dos teus relatórios) e tipo (custos de mudança, efeitos de
rede, ativos intangíveis, vantagem de custos, escala eficiente), sugerido a partir das menções nos teus relatórios, da
indústria e das margens. Tu confirmas os tipos.

É uma grelha de trabalho com cortes arbitrários, não uma avaliação definitiva.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

NAN = float("nan")


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and not math.isinf(v)


def _pct(v: float, casas: int = 1) -> str:
    return f"{v * 100:.{casas}f}%".replace(".", ",")


def _dinheiro(v: float) -> str:
    for lim, suf in ((1e9, " mil M"), (1e6, " M"), (1e3, " mil")):
        if abs(v) >= lim:
            return f"{v / lim:.1f}{suf}".replace(".", ",")
    return f"{v:.0f}"


# ---------------------------------------------------------------------------
# Dirigentes
# ---------------------------------------------------------------------------
def dirigentes(info: dict) -> dict:
    """Presidente do conselho, CEO e CFO a partir de companyOfficers do Yahoo."""
    out = {"presidente": None, "ceo": None, "cfo": None}
    for o in info.get("companyOfficers") or []:
        titulo, nome = str(o.get("title") or ""), o.get("name")
        if not nome:
            continue
        t = titulo.lower()
        pessoa = {"nome": re.sub(r"\s+", " ", str(nome)).strip(), "cargo": titulo, "idade": o.get("age"),
                  "remuneracao": o.get("totalPay"), "nascimento": o.get("yearBorn")}
        if out["ceo"] is None and ("chief executive" in t or re.search(r"\bceo\b", t)) and "ceo of" not in t:
            out["ceo"] = pessoa
        if out["cfo"] is None and ("chief financial" in t or re.search(r"\bcfo\b", t)):
            out["cfo"] = pessoa
        if out["presidente"] is None and "chair" in t and "vice chair" not in t.replace("vice-chair", "vice chair"):
            out["presidente"] = pessoa
    return out


def apelido(nome: str) -> str:
    """Último apelido, sem títulos nem sufixos ("Mr. Satya Nadella" -> "Nadella"; "Bradford L. Smith LCA" -> "Smith")."""
    partes = [p for p in re.sub(r"[.,]", " ", nome).split() if p.lower() not in {"mr", "ms", "mrs", "dr", "jr", "sr", "ii", "iii", "iv", "lca", "cpa", "phd"}]
    return partes[-1] if partes else nome


# ---------------------------------------------------------------------------
# Transações dos insiders
# ---------------------------------------------------------------------------
def resumo_transacoes(transacoes: list[dict], hoje: datetime | None = None, dias: int = 365) -> dict:
    """transacoes: registos do Yahoo com "data", "insider", "cargo", "texto", "acoes", "valor". Compras e vendas em mercado;
    prémios em ações, doações e exercícios não contam como decisão de investimento."""
    hoje = hoje or datetime.now()
    limite = hoje - timedelta(days=dias)
    compras = vendas = 0.0
    n_c = n_v = 0
    por_pessoa: dict[str, dict] = {}
    for t in transacoes:
        try:
            d = datetime.fromisoformat(str(t.get("data"))[:10])
        except ValueError:
            continue
        if d < limite:
            continue
        texto = str(t.get("texto") or "").lower()
        valor = float(t.get("valor") or 0) if _ok(t.get("valor") or 0) else 0.0
        tipo = "compra" if texto.startswith("purchase") else "venda" if texto.startswith("sale") else None
        if tipo is None:
            continue
        p = por_pessoa.setdefault(str(t.get("insider") or "?"), {"cargo": t.get("cargo"), "compras": 0.0, "vendas": 0.0})
        if tipo == "compra":
            compras += valor; n_c += 1; p["compras"] += valor
        else:
            vendas += valor; n_v += 1; p["vendas"] += valor
    return {"compras": compras, "vendas": vendas, "n_compras": n_c, "n_vendas": n_v, "por_pessoa": por_pessoa, "dias": dias}


# ---------------------------------------------------------------------------
# Grelha da gestão
# ---------------------------------------------------------------------------
def _risco_iss(v) -> int | None:
    if not _ok(v):
        return None
    return 2 if v <= 3 else 1 if v <= 6 else 0


def grelha_gestao(info: dict, transacoes: dict, valores: dict | None, evid: dict, manual: dict | None = None) -> dict:
    """
    info: info do Yahoo; transacoes: resumo_transacoes(); valores: métricas da análise fundamental (spread ROIC−WACC,
    variação do número de ações) ou None; evid: evidências da wiki por grupo; manual: {chave: {"pontos": 0-2, "nota": str}}.
    """
    manual = manual or {}
    cap = info.get("marketCap")
    grande = _ok(cap) and cap >= 100e9
    ger = dirigentes(info)
    itens = []

    def item(chave, criterio, pontos, leitura, fonte, auto=True):
        m = manual.get(chave) or {}
        substituido = m.get("pontos") in (0, 1, 2)
        itens.append({"chave": chave, "criterio": criterio, "pontos_auto": pontos,
                      "pontos": m["pontos"] if substituido else pontos, "substituido": substituido and auto,
                      "nota_manual": m.get("nota") or "", "leitura": leitura, "fonte": fonte, "automatico": auto})

    # 1. Alinhamento: participação dos insiders (em empresas gigantes, 0,1% já é muito dinheiro)
    ins = info.get("heldPercentInsiders")
    if _ok(ins):
        cortes = (0.01, 0.001) if grande else (0.05, 0.01)
        p = 2 if ins >= cortes[0] else 1 if ins >= cortes[1] else 0
        item("alinhamento", "Alinhamento: capital detido pelos gestores e administradores", p,
             f"{_pct(ins, 2)} do capital (cortes {_pct(cortes[0], 1)} / {_pct(cortes[1], 1)} para "
             f"{'empresas acima de 100 mil M' if grande else 'empresas abaixo de 100 mil M'})", "Yahoo (heldPercentInsiders)")
    else:
        item("alinhamento", "Alinhamento: capital detido pelos gestores e administradores", None, "sem dados", "Yahoo")

    # 2. Transações dos insiders nos últimos 12 meses
    c, v = transacoes.get("compras", 0.0), transacoes.get("vendas", 0.0)
    if transacoes.get("n_compras", 0) + transacoes.get("n_vendas", 0) == 0:
        p, leit = 1, "sem compras nem vendas em mercado nos últimos 12 meses"
    else:
        peso = v / cap if _ok(cap) and cap > 0 else NAN
        if c > 0 and c >= v:
            p = 2
        elif _ok(peso) and peso <= 0.0005 or c > 0:
            p = 1
        else:
            p = 0
        leit = (f"{transacoes['n_compras']} compras ({_dinheiro(c)}) e {transacoes['n_vendas']} vendas ({_dinheiro(v)}) em 12 meses"
                + (f"; vendas = {_pct(peso, 3)} da capitalização" if _ok(peso) else ""))
    item("transacoes", "Compras e vendas de ações pelos insiders (12 meses)", p, leit, "Yahoo (insider transactions)")

    # 3-5. Riscos de governo da ISS (1 = melhor, 10 = pior)
    item("remuneracao", "Remuneração dos gestores (risco ISS)", _risco_iss(info.get("compensationRisk")),
         f"risco {info.get('compensationRisk', 'n/d')} em 10" + (
             f"; CEO recebeu {_dinheiro(ger['ceo']['remuneracao'])}" if ger["ceo"] and _ok(ger["ceo"].get("remuneracao")) else ""),
         "ISS via Yahoo (compensationRisk)")
    cons = [info.get(k) for k in ("boardRisk", "shareHolderRightsRisk") if _ok(info.get(k))]
    item("conselho", "Conselho de administração e direitos dos acionistas (risco ISS)",
         _risco_iss(sum(cons) / len(cons)) if cons else None,
         f"conselho {info.get('boardRisk', 'n/d')} e direitos dos acionistas {info.get('shareHolderRightsRisk', 'n/d')} em 10",
         "ISS via Yahoo (boardRisk, shareHolderRightsRisk)")
    item("auditoria", "Auditoria e controlo (risco ISS)", _risco_iss(info.get("auditRisk")),
         f"risco {info.get('auditRisk', 'n/d')} em 10", "ISS via Yahoo (auditRisk)")

    # 6. Separação entre presidente do conselho e CEO
    pres, ceo = ger["presidente"], ger["ceo"]
    if pres and ceo:
        mesma = apelido(pres["nome"]).lower() == apelido(ceo["nome"]).lower()
        item("separacao", "Separação entre presidente do conselho e CEO", 1 if mesma else 2,
             f"{ceo['nome']} acumula presidente e CEO" if mesma else f"presidente {pres['nome']}, CEO {ceo['nome']}",
             "Yahoo (companyOfficers)")
    else:
        item("separacao", "Separação entre presidente do conselho e CEO", None,
             "o Yahoo não indica o presidente do conselho", "Yahoo (companyOfficers)")

    # 7. Alocação de capital: retorno acima do custo do capital e sem diluir os acionistas
    if valores:
        sp, va = valores.get("spread"), valores.get("var_acoes")
        partes, p = [], 0
        if _ok(sp):
            p += 1 if sp > 0 else 0
            partes.append(f"ROIC {'acima' if sp > 0 else 'abaixo'} do WACC em {_pct(abs(sp))}")
        if _ok(va):
            p += 1 if va <= 0 else 0
            partes.append(f"número de ações {'a descer' if va <= 0 else 'a subir'} ({'+' if va > 0 else ''}{_pct(va)} no período)")
        item("alocacao", "Alocação de capital (retorno acima do custo do capital, sem diluição)",
             p if partes else None, "; ".join(partes) or "sem dados", "Análise fundamental (4 exercícios)")
    else:
        item("alocacao", "Alocação de capital (retorno acima do custo do capital, sem diluição)", None,
             "análise fundamental ainda não calculada", "Análise fundamental")

    # 8-9. Julgamento teu, com a evidência dos relatórios
    cred = evid.get("credibilidade", {})
    item("credibilidade", "Credibilidade: cumpre o que promete (guidance, prazos, metas)", None,
         f"{cred.get('mencoes', 0)} menções a guidance e metas nos teus relatórios", "Tu (com a evidência da wiki)", auto=False)
    hist = evid.get("historial", {})
    item("historial", "Historial e competência do CEO e do CFO", None,
         f"{hist.get('mencoes', 0)} menções aos dirigentes nos teus relatórios", "Tu (com a evidência da wiki)", auto=False)

    avaliados = [i for i in itens if i["pontos"] is not None]
    total = sum(i["pontos"] for i in avaliados)
    maximo = 2 * len(avaliados)
    nota = round(total / maximo * 10, 1) if maximo else None
    classe = None if nota is None else "forte" if nota >= 7 else "adequada" if nota >= 4 else "fraca"
    return {"dirigentes": ger, "itens": itens, "pontos": total, "maximo": maximo, "nota": nota, "classe": classe,
            "n_avaliados": len(avaliados), "n_total": len(itens)}


# ---------------------------------------------------------------------------
# Moat
# ---------------------------------------------------------------------------
TIPOS_MOAT = {
    "custos_mudanca": ("Custos de mudança", "O cliente perde tempo, dinheiro ou risco ao trocar de fornecedor.",
                       ["custos de mudança", "custo de mudança", "custos de troca", "switching cost", "lock-in", "aprisionamento",
                        "integração profunda", "enraizad"]),
    "efeitos_rede": ("Efeitos de rede", "O produto vale mais para cada utilizador quanto mais utilizadores tem.",
                     ["efeito de rede", "efeitos de rede", "network effect", "dois lados", "two-sided"]),
    "intangiveis": ("Ativos intangíveis", "Marcas, patentes, licenças ou autorizações regulatórias difíceis de copiar.",
                    ["poder de marca", "marca forte", "marcas", "brand", "patente", "patent", "licença", "propriedade intelectual",
                     "intangíve", "aprovação regulat"]),
    "custos": ("Vantagem de custos", "Produz ou distribui mais barato do que a concorrência (escala, processo, localização).",
               ["vantagem de custo", "vantagens de custo", "custo unitário", "economias de escala", "economies of scale",
                "menor custo", "low-cost", "baixo custo"]),
    "escala_eficiente": ("Escala eficiente", "O mercado só comporta poucos operadores rentáveis; um novo entrante destruiria o retorno de todos.",
                         ["escala eficiente", "efficient scale", "monopólio", "duopólio", "oligopólio", "barreira à entrada",
                          "barreiras à entrada", "irreplicável", "concessão"]),
}

# Indústria do Yahoo (palavras-chave) -> tipos de moat típicos
INDUSTRIA_MOAT = [
    (("software", "information technology services"), ("custos_mudanca",)),
    (("credit services", "financial data", "capital markets", "internet content", "stock exchange"), ("efeitos_rede",)),
    (("drug manufacturers", "biotechnology", "beverages", "household", "luxury", "packaged foods", "tobacco", "confectioners",
      "personal products", "apparel"), ("intangiveis",)),
    (("medical instruments", "medical devices", "diagnostics"), ("intangiveis", "custos_mudanca")),
    (("semiconductor",), ("intangiveis", "custos_mudanca")),
    (("waste management", "railroads", "utilities", "pipelines", "airports", "towers", "reit - specialty"), ("escala_eficiente",)),
    (("discount stores", "internet retail", "specialty retail", "grocery", "steel", "trucking", "airlines"), ("custos",)),
]


def termos_moat() -> dict[str, list[str]]:
    """Termos a procurar nos relatórios para cada tipo de moat (a avaliação está em moat.py)."""
    return {k: v[2] for k, v in TIPOS_MOAT.items()}
