"""
Leitura dos dois formatos fixos dos relatórios do Cowork (definidos a 10/10/2026; nenhum relatório anterior os tem):

  1. Ficha de dados: <div class="ficha" data-ficha="v1"> com cinco <p> (recomendação, valor intrínseco, preço-alvo,
     tese, fosso). É a fonte de verdade da app. Outra versão de data-ficha não é interpretada: fica um aviso.
  2. Linhas «Filtros:» e «Cenários:» do «Resumo de Baseline» / «Novo Resumo de Baseline». São a única origem das notas
     de gestão e de governação e dos cenários do DCF.

Regras: campo em falta ou escrito «lacuna» fica None (lacuna visível, nunca zero nem estimativa); o que não corresponde
ao modelo não se adivinha. Não depende do FastAPI: pode ser testado à parte.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata

VERSAO_FICHA = "v1"
RECOMENDACOES = ("COMPRAR", "ACUMULAR", "MANTER", "REDUZIR", "VENDER")
TESES = {"mantem-se": "intacta", "reforca-se": "reforcada", "enfraquece": "enfraquecida", "quebrada": "quebrada"}  # sem acentos
RESULTADOS = ("merecem confiança com reservas", "merecem confiança", "não merecem")  # o mais longo primeiro

# Número com vírgula ou ponto decimal, milhares separados por espaço (2 889,5) e sinal opcional
NUM = r"-?(?:\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)?"
MOEDA = r"[A-Z]{3}|GBp"
SEPARADOR = re.compile(r"\s*[·;]\s*")
PROIBIDO_NA_FICHA = re.compile(r"(?i)\b(bear|bull|ponderad|pessimista|otimista|optimista|analista|consenso)")


def _num(s: str) -> float:
    return float(re.sub(r"[  ]", "", s).replace(",", "."))


def _texto(fragmento_html: str) -> str:
    t = _html.unescape(re.sub(r"<[^>]+>", " ", fragmento_html))
    return re.sub(r"\s+", " ", t).strip()


def _sem_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


# ---------------------------------------------------------------------------
# 1. Ficha de dados
# ---------------------------------------------------------------------------
RE_CAIXA = re.compile(r'(?is)<div\b[^>]*\bdata-ficha\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</div>')


def localizar_ficha(h: str) -> re.Match | None:
    return RE_CAIXA.search(h or "")


def ler_ficha(h: str) -> dict:
    """
    estado: "ausente" (relatório sem Ficha: formato antigo), "versao_desconhecida" (não interpretada, com aviso) ou "ok".
    campos: recomendacao, valor_intrinseco {valor, moeda}, preco_alvo {valor, moeda}, tese, fosso — None = lacuna.
    """
    m = localizar_ficha(h)
    if not m:
        return {"estado": "ausente", "versao": None, "campos": {}, "lacunas": [], "avisos": []}
    versao = m.group(1).strip()
    if versao != VERSAO_FICHA:
        return {"estado": "versao_desconhecida", "versao": versao, "campos": {}, "lacunas": [],
                "avisos": [f"Ficha de dados na versão «{versao}», que a app não conhece (só lê «{VERSAO_FICHA}»): "
                           "não foi interpretada. Confirma o relatório."]}

    campos = {"recomendacao": None, "valor_intrinseco": None, "preco_alvo": None, "tese": None, "fosso": None}
    linhas = {}
    avisos = []
    paragrafos = [_texto(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", m.group(2))]
    if len(paragrafos) != 5:
        avisos.append(f"A Ficha tem {len(paragrafos)} linhas em vez de 5.")
    for p in paragrafos:
        if PROIBIDO_NA_FICHA.search(p):
            avisos.append(f"Linha recusada (valores bear, bull, ponderado ou de analistas não entram na Ficha): «{p}»")
            continue
        if r := re.fullmatch(r"(?i)recomenda[çc][ãa]o\s*:\s*(\S.*)", p):
            palavra = r.group(1).strip().upper()
            if palavra in RECOMENDACOES:
                campos["recomendacao"] = palavra
            else:
                avisos.append(f"Recomendação fora das cinco palavras permitidas: «{r.group(1).strip()}» (fica lacuna).")
            linhas["recomendacao"] = p
        elif r := re.fullmatch(rf"(?i)valor intr[íi]nseco \(cen[áa]rio base\)\s*:\s*({NUM})\s*({MOEDA})", p):
            campos["valor_intrinseco"] = _valor_moeda(r.group(1), r.group(2))
            linhas["valor_intrinseco"] = p
        elif r := re.fullmatch(rf"(?i)pre[çc]o-alvo a 12 meses\s*:\s*({NUM})\s*({MOEDA})", p):
            campos["preco_alvo"] = _valor_moeda(r.group(1), r.group(2))
            linhas["preco_alvo"] = p
        elif r := re.fullmatch(r"(?i)tese (mant[ée]m-se|refor[çc]a-se|enfraquece|quebrada)", p):
            campos["tese"] = TESES.get(_sem_acentos(r.group(1)))
            linhas["tese"] = p
        elif r := re.fullmatch(rf"(?i)fosso\s*:\s*({NUM})\s*/\s*10", p):
            v = _num(r.group(1))
            if 0 <= v <= 10:
                campos["fosso"] = v
            else:
                avisos.append(f"Fosso fora de 0 a 10: «{p}» (fica lacuna).")
            linhas["fosso"] = p
        else:
            avisos.append(f"Linha da Ficha que não corresponde ao modelo: «{p}» (não interpretada).")
    lacunas = [k for k, v in campos.items() if v is None]
    return {"estado": "ok", "versao": versao, "campos": campos, "linhas": linhas, "lacunas": lacunas, "avisos": avisos}


def _valor_moeda(numero: str, moeda: str) -> dict:
    v = _num(numero)
    if moeda == "GBp":  # pence -> libras, como a cotação na app
        return {"valor": v / 100, "moeda": "GBP", "original": f"{numero} GBp"}
    return {"valor": v, "moeda": moeda.upper()}


def sem_ficha(h: str) -> str:
    """HTML sem a caixa da Ficha (para uma Ficha de versão desconhecida não ser lida pelo leitor de texto)."""
    return RE_CAIXA.sub(" ", h or "")


# ---------------------------------------------------------------------------
# 2. Linhas «Filtros:» e «Cenários:» do Resumo de Baseline
# ---------------------------------------------------------------------------
RE_TITULO_BASELINE = re.compile(r"(?i)(novo\s+)?resumo\s+de\s+baseline")

TENDENCIAS_ROIC = {"ascendente": "ascendente", "estavel": "estável", "descendente": "descendente"}  # chave sem acentos


def _roic(r: re.Match) -> dict:
    """«ROIC 13,0% (média 5a 1,4%; tendência 3a ascendente)». Dentro dos parênteses, separados por «;», os campos
    OPCIONAIS «média 5a X%» e «tendência 3a ascendente|estável|descendente», por qualquer ordem. Outra coisa = erro."""
    out = {"ttm": _num(r.group(1)) / 100, "media_5a": None, "tendencia_3a": None}
    for parte in [p.strip() for p in (r.group(2) or "").split(";") if p.strip()]:
        if m := re.fullmatch(rf"(?i)m[ée]dia\s*5\s*a(?:nos)?\s+({NUM})\s*%", parte):
            out["media_5a"] = _num(m.group(1)) / 100
        elif m := re.fullmatch(r"(?i)tend[êe]ncia\s*3\s*a(?:nos)?\s+(ascendente|est[áa]vel|descendente)", parte):
            out["tendencia_3a"] = TENDENCIAS_ROIC[_sem_acentos(m.group(1))]
        else:
            raise ValueError(parte)
    return out


def _data(r: re.Match) -> str:
    from datetime import date
    return date(int(r.group(3)), int(r.group(2)), int(r.group(1))).isoformat()  # data impossível -> ValueError


CAMPOS_FILTROS = {
    "fosso": (rf"(?i)fosso\s+({NUM})\s*/\s*10", lambda r: _num(r.group(1))),
    "gestao": (rf"(?i)gest[ãa]o\s+({NUM})\s*/\s*5", lambda r: _num(r.group(1))),
    "governacao": (rf"(?i)governa[çc][ãa]o\s+({NUM})\s*/\s*5", lambda r: _num(r.group(1))),
    "resultados": (r"(?i)resultados\s*:\s*(merecem confian[çc]a com reservas|merecem confian[çc]a|n[ãa]o merecem)",
                   lambda r: next(x for x in RESULTADOS if _sem_acentos(x) == _sem_acentos(r.group(1)))),
    "roic": (rf"(?i)roic\s+({NUM})\s*%(?:\s*\(([^()]*)\))?", _roic),
    "wacc": (rf"(?i)wacc\s+({NUM})\s*%", lambda r: _num(r.group(1)) / 100),
}
CAMPOS_CENARIOS = {
    **{c: (rf"(?i){c}\s+({NUM})(?:\s*(?:{MOEDA}))?\s*\(\s*({NUM})\s*%\s*\)",
           lambda r: {"valor": _num(r.group(1)), "prob": _num(r.group(2)) / 100}) for c in ("bear", "base", "bull")},
    "ponderado": (rf"(?i)ponderado\s+({NUM})(?:\s*({MOEDA}))?", lambda r: {"valor": _num(r.group(1)), "moeda": r.group(2)}),
    "preco": (rf"(?i)pre[çc]o\s+({NUM})\s*({MOEDA})", lambda r: {"valor": _num(r.group(1)), "moeda": r.group(2)}),
    "desconto": (rf"(?i)desconto ao ponderado\s+({NUM})\s*%", lambda r: _num(r.group(1)) / 100),
    "assimetria": (rf"(?i)assimetria\s+({NUM})", lambda r: _num(r.group(1))),
    "data": (r"(?i)data\s+(\d{2})/(\d{2})/(\d{4})", _data),  # OPCIONAL: data dos cenários
}
OPCIONAIS = {"data"}  # campos que podem faltar sem a linha deixar de ser verificável
RE_LACUNA = re.compile(r"(?i)^(fosso|gest[ãa]o|governa[çc][ãa]o|resultados|roic|wacc|bear|base|bull|ponderado|pre[çc]o|"
                       r"desconto ao ponderado|assimetria)\s*:?\s*lacuna$")


def _segmentos(conteudo: str) -> list[str]:
    """Divide nos separadores « · » e «;» que estão FORA de parênteses (o «;» dentro dos do ROIC separa sub-campos)."""
    segs, nivel, atual = [], 0, ""
    for ch in conteudo.strip().rstrip("."):
        nivel += (ch == "(") - (ch == ")" and nivel > 0)
        if nivel == 0 and ch in "·;":
            segs.append(atual)
            atual = ""
        else:
            atual += ch
    segs.append(atual)
    return [s.strip() for s in segs if s.strip()]


def _ler_linha(conteudo: str, modelo: dict) -> tuple[dict, list[str]]:
    """Campos lidos (None = ausente ou lacuna) e segmentos que não correspondem ao modelo."""
    valores = {k: None for k in modelo}
    estranhos = []
    for seg in _segmentos(conteudo):
        lac = RE_LACUNA.match(seg)
        if lac:
            continue  # campo escrito «lacuna»: fica None
        for chave, (rx, conv) in modelo.items():
            r = re.fullmatch(rx, seg)
            if r:
                if valores[chave] is not None:
                    estranhos.append(f"{seg} (campo repetido)")
                else:
                    try:
                        valores[chave] = conv(r)
                    except ValueError:  # sub-campo desconhecido ou data impossível: não se adivinha
                        estranhos.append(seg)
                break
        else:
            estranhos.append(seg)
    return valores, estranhos


def ler_baseline(texto: str) -> dict:
    """
    estado: "ausente" (sem as linhas: relatórios anteriores a 10/10/2026), "nao_reconhecido" (linha fora do modelo: não
    se adivinha) ou "ok". verificavel = True só com todos os campos preenchidos.
    """
    vazio = {"estado": "ausente", "filtros": None, "cenarios": None, "faltam": [], "verificavel": False, "linhas": {}, "erros": []}
    titulos = list(RE_TITULO_BASELINE.finditer(texto or ""))
    if not titulos:
        return vazio
    seccao = texto[titulos[-1].end():titulos[-1].end() + 4000]  # o último título é o baseline mais recente do relatório
    linhas = {}
    for chave, rotulo in (("filtros", r"filtros"), ("cenarios", r"cen[áa]rios")):
        r = re.search(rf"(?im)^\s*{rotulo}\s*:\s*(.+)$", seccao)
        if r:
            linhas[chave] = r.group(1).strip()
    if not linhas:
        return vazio

    filtros, est_f = _ler_linha(linhas["filtros"], CAMPOS_FILTROS) if "filtros" in linhas else (None, [])
    cenarios, est_c = _ler_linha(linhas["cenarios"], CAMPOS_CENARIOS) if "cenarios" in linhas else (None, [])
    if cenarios is not None:
        # Os três cenários não trazem moeda: herdam a do «ponderado» da mesma linha (valores por ação, na moeda da
        # cotação). Ponderado sem moeda -> a moeda dos cenários fica lacuna.
        moeda = (cenarios.get("ponderado") or {}).get("moeda")
        cenarios["moeda"] = moeda
        for c in ("bear", "base", "bull"):
            if cenarios.get(c):
                cenarios[c]["moeda"] = moeda
    erros = [f"Filtros: «{e}»" for e in est_f] + [f"Cenários: «{e}»" for e in est_c]
    faltam = ([] if "filtros" in linhas else ["linha Filtros"]) + ([] if "cenarios" in linhas else ["linha Cenários"])
    faltam += [f"filtros.{k}" for k, v in (filtros or {}).items() if v is None]
    faltam += [f"cenarios.{k}" for k, v in (cenarios or {}).items() if v is None and k not in OPCIONAIS]
    estado = "nao_reconhecido" if erros else "ok"
    return {"estado": estado, "filtros": filtros, "cenarios": cenarios, "faltam": faltam,
            "verificavel": estado == "ok" and not faltam, "linhas": linhas, "erros": erros}
