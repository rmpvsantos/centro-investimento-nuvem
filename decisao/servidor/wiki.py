"""
Wiki: todo o conhecimento sobre cada empresa num só sítio.

  1. Relatórios HTML da pasta "Estudos Empresas-Relatorios" (lidos diretamente, nunca copiados): metadados, texto integral,
     veredito da tese, recomendação e dados extraídos (preço-alvo, valor intrínseco, nota do fosso), sempre com o excerto
     de onde saíram para poderem ser verificados.
  2. Análises feitas na app (dados/analises/<TICKER>/<data>.json): cada decisão guardada passa a fazer parte da wiki.
  3. Notas e avaliações manuais (dados/notas/<TICKER>.json).

Não depende do FastAPI: pode ser testado à parte.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import ficha as FI
from config import pasta_dados, pasta_relatorios  # noqa: F401  (pasta_relatorios usada também pelo servidor)

IGNORAR = {"app-investimentos", "triagem_acoes", "decisao_investimento", "node_modules", ".git", "_TEMPLATES", "APP"}

# Pastas-contentor: as empresas estão numa subpasta (CARTEIRA INVESTIMENTOS\MSFT\...). Funciona com a organização atual
# (desde 28/09/2026) e com a anterior (empresas da carteira à raiz, 1 EM ANALISE, 2 VARIOS).
CONTENTORES = {"carteira investimentos": "carteira", "carteira": "carteira", "em observacao": "analise", "1 em analise": "analise",
               "em analise": "analise", "analises antigas": "varios", "2 varios": "varios"}
CATEGORIAS = {"_FERRAMENTAS": "ferramenta", "_REVER_DUPLICADOS": "duplicado", "Claude outputs": "outros"}
# Onde gravar os relatórios de cada lista (a primeira pasta que existir)
PASTAS_DAS_LISTAS = {"carteira": ["CARTEIRA INVESTIMENTOS", ""], "observacao": ["EM OBSERVAÇÃO", "1 EM ANALISE"]}
TEXTO_MAX = 300_000

# Pasta da empresa nos relatórios -> ticker do Yahoo, quando não coincidem com o ticker dos nomes dos ficheiros
TICKER_YAHOO = {"VISA": "V", "BONEX": "BONEX.ST", "BONESUPPORT": "BONEX.ST", "ABB": "ABB.ST", "ATCO": "ATCO-A.ST"}

VEREDITOS = (  # por prioridade: o primeiro padrão não negado que aparecer ganha
    ("quebrada", re.compile(r"\b(quebrad[ao]|broken|invalidad[ao])", re.I)),
    ("enfraquecida", re.compile(r"\b(enfraquec\w*|weaken\w*)", re.I)),
    ("reforcada", re.compile(r"\b(refor[çc]ad[ao]|refor[çc]a-se|refor[çc]ou-se|strengthen\w*)", re.I)),
    ("intacta", re.compile(r"\b(intact[ao]?|mant[ée]m-se|manteve-se|permanece)", re.I)),
)
PALAVRA_REC = r"(?:comprar|acumular|manter|reduzir|vender|evitar|aguardar|neutr[oa]l?)"
RE_REC = re.compile(rf"(?:recomendação|recomendacao|veredito|veredicto)\s*:?\s*({PALAVRA_REC}(?:\s*[/,·]\s*{PALAVRA_REC})*)", re.I)
# Milhares separados (2 889 / 1.234,5) só com grupos completos; senão decimal com ponto ou vírgula ($17.31, 16,41)
NUM = r"(\d{1,3}(?:[ . ]\d{3})+(?:,\d+)?(?![.,]?\d)|\d+(?:[.,]\d+)?)"
RE_ALVO = re.compile(r"pre[çc]o[- ]alvo", re.I)
RE_VALOR = re.compile(r"valor intr[íi]nseco|\bDCF\s+base\b", re.I)
# Ficha de dados normalizada no topo dos relatórios (desde 10/10/2026): tem prioridade sobre tudo o resto
RE_FICHA_VALOR = re.compile(rf"(?i)valor intr[íi]nseco \(cen[áa]rio base\)\s*:\s*{NUM}")
RE_FICHA_ALVO = re.compile(rf"(?i)pre[çc]o-alvo a 12 meses\s*:\s*{NUM}")
RE_FICHA_REC = re.compile(r"(?i)recomenda[çc][ãa]o\s*:\s*(comprar|acumular|manter|reduzir|vender)\b")
# Valores que não são os do Rui: analistas e bancos, cenários extremos, preços de mercado, recompras
CONTEXTO_ALHEIO = re.compile(
    r"(?i)analista|consenso|m[ée]dio|casas? de investimento|\bbancos?\b|rating|corretora|research|"
    r"pessimista|otimista|optimista|\bbear\b|\bbull\b|cota[çc][ãa]o|recompra|buyback|"
    r"morgan|goldman|jpmorgan|\bubs\b|barclays|\bciti|bernstein|jefferies|wedbush|mizuho|truist|stifel|evercore|bofa")
BANCOS = re.compile(r"(?i)morgan|goldman|jpmorgan|\bubs\b|barclays|\bciti|bernstein|jefferies|wedbush|mizuho|truist|"
                    r"stifel|evercore|bofa|analista|\bbanco\b|corretora")
# Recomendações fora da escala da app -> nível equivalente (o texto original fica guardado)
REC_EQUIVALENTE = {"NEUTRO": "MANTER", "NEUTRA": "MANTER", "NEUTRAL": "MANTER", "AGUARDAR": "MANTER", "EVITAR": "VENDER"}
RE_FOSSO = re.compile(r"\b(fosso|moat)\b", re.I)
RE_NOTA10 = re.compile(r"(\d{1,2}(?:[,.]\d)?)\s*/\s*10\b")


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def normalizar(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


def html_para_texto(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1>", " ", h)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|section|td|th)>", " \n ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    t = _html.unescape(h)
    t = re.sub(r"[ \t\r\f\v ]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return re.sub(r"\n{2,}", "\n", t).strip()


def _numero(txt: str) -> float | None:
    s = txt.replace(" ", " ").strip()
    if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+(?:,\d+)?", s):  # 1 234,5 ou 1.234,5
        s = s.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        s = s.replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _excerto(texto: str, i: int, antes: int = 60, depois: int = 200) -> str:
    a, b = max(0, i - antes), min(len(texto), i + depois)
    return ("…" if a else "") + re.sub(r"\s+", " ", texto[a:b]).strip() + ("…" if b < len(texto) else "")


def _negado(texto: str, i: int) -> bool:
    return bool(re.search(r"(^|[^\w])(não|nem|not)\s+(\w+\s+)?$", texto[max(0, i - 16):i].lower()))


# ---------------------------------------------------------------------------
# Extração a partir do texto de um relatório
# ---------------------------------------------------------------------------
def _primeiro_veredito(janela: str) -> str | None:
    melhor = None
    for veredito, rx in VEREDITOS:
        for m in rx.finditer(janela):
            if _negado(janela, m.start()):
                continue
            if melhor is None or m.start() < melhor[1]:
                melhor = (veredito, m.start())
            break
    return melhor[0] if melhor else None


def extrair_veredito(texto: str) -> tuple[str | None, str | None]:
    """1.º frases explícitas ("Tese mantém-se"); 2.º janelas junto a "Estado da tese" / "Veredito"."""
    for ancora, alcance in ((r"\btese\b", 60), (r"(estado da tese|veredicto|veredito|thesis check|verificação da tese)", 400)):
        for m in re.finditer(ancora, texto, re.I):
            v = _primeiro_veredito(texto[m.start():m.start() + alcance])
            if v:
                return v, _excerto(texto, m.start(), 0, 320)
    return None, None


def extrair_recomendacao(texto: str) -> str | None:
    m = RE_FICHA_REC.search(texto) or RE_REC.search(texto)
    return re.sub(r"\s+", " ", m.group(1)).upper() if m else None


def nivel_recomendacao(rec: str | None) -> str | None:
    """Primeira palavra da recomendação na escala da app (COMPRAR, ACUMULAR, MANTER, REDUZIR, VENDER)."""
    if not rec:
        return None
    p = re.split(r"[\s/,·]+", rec.strip().upper())[0]
    return REC_EQUIVALENTE.get(p, p) if p in REC_EQUIVALENTE or p in ("COMPRAR", "ACUMULAR", "MANTER", "REDUZIR", "VENDER") else None


MOEDA_ANTES = r"[$€£]\s*"
MOEDA_DEPOIS = r"\s*(?:\$|€|£|USD|EUR|GBP|GBp|CAD|SEK|CHF|DKK|d[óo]lares|euros|libras|coroas|pence|p\b)"


def _valor_apos(texto: str, rx: re.Pattern, minimo: float = 1.0, ficha: re.Pattern | None = None) -> dict | None:
    """
    1.º a linha da ficha de dados, se existir ("Valor intrínseco (cenário base): 215 USD").
    2.º o primeiro valor monetário logo a seguir ao rótulo (até 90 carateres). O valor tem de trazer moeda ($ 295,
    295 dólares) ou estar colado ao rótulo numa célula de tabela ("Preço-alvo 12M 205"). Numa revisão ("revisto de 230
    para 295") fica o destino. Ignora prazos ("12 meses", "12M") e rótulos "anterior".
    Descarta o que não é a opinião do Rui: valores de analistas e bancos, cenários pessimista e otimista, cotações e
    preços de recompras (ver CONTEXTO_ALHEIO), montantes em mil milhões, percentagens e números do índice do relatório.
    """
    def moeda(trecho: str) -> str | None:  # moeda escrita junto ao número (para converter antes de comparar com a cotação)
        t = trecho.lower()
        for cod, rx in (("CAD", r"cad|c\$|d[óo]lares canadianos"), ("SEK", r"sek|coroas? suecas?"), ("DKK", r"dkk|coroas? dinamarquesas?"),
                        ("CHF", r"chf|francos?"), ("GBP", r"gbp|£|libras?|pence|\d\s*p\b"), ("EUR", r"eur|€|euros?"),
                        ("USD", r"usd|us\$|\$|d[óo]lares?")):
            if re.search(rx, t):
                return cod
        return None

    def em_pence(depois: str) -> bool:  # relatórios de empresas de Londres dão valores em pence ("2 889 pence")
        return bool(re.match(r"\s*(pence|gbp\b|p\b)", depois) and not re.match(r"\s*(pp|p\.p)", depois, re.I))

    if ficha:
        f = ficha.search(texto)
        if f and (v := _numero(f.group(1))) is not None and v >= minimo:
            v = v / 100 if em_pence(texto[f.end(1):f.end(1) + 8]) else v
            return {"valor": v, "excerto": _excerto(texto, f.start()), "origem": "ficha",
                    "moeda": moeda(texto[f.start(1) - 4:f.end(1) + 12])}
    for m in rx.finditer(texto):
        antes = texto[max(0, m.start() - 250):m.start()]
        if (re.search(r"(?i)n[ãa]o [ée]|n[ãa]o constitui", antes[-20:]) or CONTEXTO_ALHEIO.search(antes[-60:])
                or BANCOS.search(antes)):
            continue
        janela = texto[m.end():m.end() + 90]
        if re.match(r"(?i)\s*(anterior|antigo|de partida|inicial|original)", janela):
            continue
        janela = re.sub(r"(?i)\b(a\s+)?(12\s*m(eses)?|doze meses|12-month|base|central|\(base\)|dcf)\b", " ", janela)
        cand = (re.match(rf"(?i)[^.]{{0,25}}?\bde\s+(?:{MOEDA_ANTES})?{NUM}{MOEDA_DEPOIS}?\s+para\s+(?:{MOEDA_ANTES})?{NUM}", janela)
                or re.search(rf"{MOEDA_ANTES}{NUM}", janela)
                or re.search(rf"{NUM}{MOEDA_DEPOIS}", janela)
                or re.match(rf"[\s:\-–·]*{NUM}(?![\d.,])(?=\s*(?:$|[(|·—–\-]|\d|p\b|pence))", janela))
        if not cand:
            continue
        grupo = cand.lastindex or 1
        if CONTEXTO_ALHEIO.search(janela[:cand.start(grupo)]):
            continue  # "Preço-alvo médio dos analistas 263", "Cenário Bear ~$175"
        depois = janela[cand.end(grupo):cand.end(grupo) + 12]
        if re.match(r"(?i)\s*(b\b|bn|bi\b|mil milh|milh|m\b|%|x\b)", depois):
            continue  # "$82.7B", "12%", "39,0x": não são valores por ação
        v = _numero(cand.group(grupo))
        if v is None or v < minimo:
            continue
        if em_pence(depois):
            v = v / 100
        return {"valor": v, "excerto": _excerto(texto, m.start()), "origem": "texto",
                "moeda": moeda(janela[max(0, cand.start(grupo) - 4):cand.end(grupo) + 14])}
    return None


def extrair_fosso(texto: str) -> dict | None:
    """Nota do fosso em "/10" junto a "fosso" ou "moat"; numa revisão ("8/10 ... 8,5/10") fica a última. Quando o
    relatório separa por camadas, prefere a linha "combinado" / "global" / "geral"."""
    agregado = re.compile(r"(?i)\b(fosso|moat)\b[^\n]{0,30}\b(combinad|global|geral|total|agregad)")
    for m in list(agregado.finditer(texto)) + list(RE_FOSSO.finditer(texto)):
        janela = texto[m.start():m.start() + 90]
        notas = RE_NOTA10.findall(janela)
        if notas:
            v = _numero(notas[-1])
            if v is not None and 0 <= v <= 10:
                return {"valor": v, "excerto": _excerto(texto, m.start(), 20, 200)}
    # Escala antiga de 0 a 5: só quando o relatório escreve «x/5» de forma explícita junto ao fosso, antes de outro
    # rótulo (para não apanhar a gestão ou a governação, que continuam de 0 a 5). Mostra-se ×2 com a marca «convertido»;
    # o valor original fica guardado. Um número sem denominador não é lido (fica lacuna).
    for m in list(agregado.finditer(texto)) + list(RE_FOSSO.finditer(texto)):
        janela = re.split(r"(?i)[·;\n]|\b(gest[ãa]o|governa[çc][ãa]o|resultados|roic|wacc|tese)\b", texto[m.end():m.end() + 60])[0]
        r = re.search(r"(?<![\d,.])(\d(?:[,.]\d)?)\s*/\s*5(?![\d,.])", janela)
        if r:
            v = _numero(r.group(1))
            if v is not None and 0 <= v <= 5:
                return {"valor": v * 2, "original": f"{r.group(1)}/5", "convertido": True,
                        "excerto": _excerto(texto, m.start(), 20, 200)}
    return None


# ---------------------------------------------------------------------------
# Índice dos relatórios
# ---------------------------------------------------------------------------
def _classificar(rel: Path) -> dict:
    partes = rel.parts
    topo, ficheiro = partes[0], partes[-1]
    contentor = CONTENTORES.get(normalizar(topo))
    if contentor:
        categoria, empresa = contentor, (partes[1] if len(partes) > 2 else None)
    elif topo in CATEGORIAS:
        categoria = CATEGORIAS[topo]
        empresa = partes[1] if len(partes) > 2 else None
    else:  # organização antiga: pastas das empresas da carteira à raiz
        categoria, empresa = ("outros", None) if len(partes) == 1 else ("carteira", topo)
    return {"categoria": categoria, "empresa": empresa, "ficheiro": ficheiro,
            "arquivado": "_ARQUIVO" in partes or ficheiro.startswith("OBSOLETO_")}


def pasta_da_empresa(caminho: str) -> str:
    """Pasta da empresa (relativa à dos relatórios) a partir do caminho de um relatório dela."""
    partes = caminho.replace("\\", "/").split("/")
    n = 2 if (CONTENTORES.get(normalizar(partes[0])) or partes[0] in CATEGORIAS) else 1
    return "/".join(partes[:n])


def pasta_da_lista(lista: str) -> str:
    """Pasta onde ficam as empresas de uma lista: a da organização atual, ou a antiga se a atual não existir."""
    raiz = pasta_relatorios()
    for p in PASTAS_DAS_LISTAS.get(lista, [""]):
        if p == "" or (raiz / p).is_dir():
            return p
    return ""


def _periodo(rel: str) -> tuple[str | None, int | None]:
    m = re.search(r"FY(\d{4})-([QH])(\d)", rel, re.I)
    if not m:
        return None, None
    ano, tipo, n = int(m.group(1)), m.group(2).upper(), int(m.group(3))
    return f"FY{ano}-{tipo}{n}", ano * 10 + (n * 2 if tipo == "H" else n)


def _tipo(ficheiro: str, categoria: str, periodo: str | None) -> str:
    if categoria == "ferramenta":
        return "ferramenta"
    if re.search(r"_iniciacao_", ficheiro, re.I):
        return "iniciacao"
    if re.search(r"_acompanhamento", ficheiro, re.I):
        return "acompanhamento"
    return "trimestral" if periodo else "fundamental"


def _ticker_ficheiro(ficheiro: str) -> str | None:
    m = re.match(r"^([A-Za-z0-9.]+?)[_-]", ficheiro.removeprefix("OBSOLETO_"))
    return m.group(1).upper() if m and len(m.group(1)) <= 6 else None


def indexar_relatorio(raiz: Path, caminho: Path) -> dict:
    rel = caminho.relative_to(raiz)
    h = caminho.read_text(encoding="utf-8", errors="replace")
    ficha = FI.ler_ficha(h)
    # Ficha de versão desconhecida: não é interpretada, nem sequer pelo leitor de texto (fica só o aviso)
    texto = html_para_texto(FI.sem_ficha(h) if ficha["estado"] == "versao_desconhecida" else h)
    c = _classificar(rel)
    periodo, ordem_periodo = _periodo(str(rel))
    tipo = _tipo(c["ficheiro"], c["categoria"], periodo)
    titulo = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    data = re.search(r"(\d{4}-\d{2}-\d{2})", c["ficheiro"])
    trimestral = tipo in ("trimestral", "acompanhamento")
    veredito, excerto_v = extrair_veredito(texto) if trimestral else (None, None)
    ferramenta = c["categoria"] == "ferramenta"
    ticker = _ticker_ficheiro(c["ficheiro"])
    extraido = {
        "veredito": veredito,
        "veredito_excerto": excerto_v,
        "recomendacao": None if ferramenta else extrair_recomendacao(texto),
        "preco_alvo": None if ferramenta else _valor_apos(texto, RE_ALVO, ficha=RE_FICHA_ALVO),
        "valor_intrinseco": None if ferramenta else _valor_apos(texto, RE_VALOR, ficha=RE_FICHA_VALOR),
        "fosso": None if ferramenta else extrair_fosso(texto),
    }
    if ficha["estado"] == "ok" and not ferramenta:
        extraido = _da_ficha(ficha)  # a Ficha é a fonte de verdade: o que lá falta fica lacuna, não se vai buscar ao texto
    return {
        **extraido,
        "formato": "ficha" if ficha["estado"] == "ok" else "antigo",
        "ficha": {k: ficha[k] for k in ("estado", "versao", "lacunas", "avisos")},
        "baseline": FI.ler_baseline(texto),
        "id": rel.as_posix(),
        "caminho": rel.as_posix(),
        "ficheiro": c["ficheiro"],
        "empresa": c["empresa"] or ticker or c["ficheiro"],
        "ticker": ticker or c["empresa"],
        "categoria": c["categoria"],
        "arquivado": c["arquivado"],
        "tipo": tipo,
        "periodo": periodo,
        "ordem_periodo": ordem_periodo,
        "data": data.group(1) if data else datetime.fromtimestamp(caminho.stat().st_mtime).date().isoformat(),
        "titulo": re.sub(r"\s+", " ", _html.unescape(titulo.group(1))).strip() if titulo else c["ficheiro"],
        "texto": texto[:TEXTO_MAX],
    }


def _da_ficha(ficha: dict) -> dict:
    """Campos do relatório a partir da Ficha de dados (mesmo formato que o leitor de texto, com origem «ficha»)."""
    c, linhas = ficha["campos"], ficha.get("linhas", {})

    def item(chave, valor):
        return {**(valor if isinstance(valor, dict) else {"valor": valor}), "origem": "ficha",
                "excerto": f"Ficha de dados: {linhas.get(chave, '')}"} if valor is not None else None

    return {
        "veredito": c["tese"], "veredito_excerto": f"Ficha de dados: {linhas['tese']}" if c["tese"] else None,
        "recomendacao": c["recomendacao"], "preco_alvo": item("preco_alvo", c["preco_alvo"]),
        "valor_intrinseco": item("valor_intrinseco", c["valor_intrinseco"]), "fosso": item("fosso", c["fosso"]),
    }


def _ordem(r: dict) -> tuple:
    return (r["data"], r.get("ordem_periodo") or 0, r["tipo"] == "acompanhamento")


_CACHE: dict = {"assinatura": None, "indice": None}


def _html_da_pasta(raiz: Path) -> list[Path]:
    """Todos os .html, sem entrar nas pastas ignoradas (o node_modules de outra app tem milhares de ficheiros)."""
    saida = []
    for pasta, subpastas, nomes in os.walk(raiz) if raiz.exists() else []:
        subpastas[:] = [d for d in subpastas if d not in IGNORAR and not d.startswith(".")]
        if Path(pasta) == raiz:
            continue  # ficheiros soltos à raiz (ex.: 0_INDICE.html) não são relatórios de uma empresa
        saida += [Path(pasta) / n for n in nomes if n.lower().endswith(".html")]
    return sorted(saida)


def _assinatura(ficheiros: list[Path]) -> tuple:
    return tuple((str(f), f.stat().st_mtime_ns) for f in ficheiros)


def construir_indice(raiz: Path | None = None) -> dict:
    """Índice completo. Reutiliza o anterior se nenhum ficheiro mudou (reindexar é instantâneo nesse caso)."""
    raiz = raiz or pasta_relatorios()
    ficheiros = _html_da_pasta(raiz)
    ass = _assinatura(ficheiros)
    if _CACHE["assinatura"] == ass and _CACHE["indice"] is not None:
        return _CACHE["indice"]

    relatorios = []
    for f in ficheiros:
        try:
            relatorios.append(indexar_relatorio(raiz, f))
        except Exception as e:  # noqa: BLE001
            relatorios.append({"id": f.relative_to(raiz).as_posix(), "caminho": f.relative_to(raiz).as_posix(),
                               "ficheiro": f.name, "erro": str(e)})

    grupos: dict[str, list[dict]] = {}
    for r in relatorios:
        if r.get("erro") or r["categoria"] == "ferramenta":
            continue
        grupos.setdefault(f"{r['categoria']}/{r['empresa']}", []).append(r)

    empresas = []
    for chave, lista in grupos.items():
        atuais = sorted((r for r in lista if not r["arquivado"]), key=_ordem)
        tickers = [r["ticker"] for r in atuais if r["ticker"] and r["ticker"] != lista[0]["empresa"]]
        ticker = tickers[0] if tickers else lista[0]["empresa"]
        com_veredito = [r for r in atuais if r["veredito"]]
        empresas.append({
            "chave": chave,
            "nome": lista[0]["empresa"],
            "ticker": ticker,
            "ticker_yahoo": TICKER_YAHOO.get(ticker) or TICKER_YAHOO.get(lista[0]["empresa"]) or ticker,
            "categoria": lista[0]["categoria"],
            "relatorios": [r["id"] for r in atuais] + [r["id"] for r in lista if r["arquivado"]],
            "ultimo": atuais[-1]["id"] if atuais else None,
            "ultimo_periodo": next((r["periodo"] for r in reversed(atuais) if r["periodo"]), None),
            "veredito": com_veredito[-1]["veredito"] if com_veredito else None,
            "veredito_periodo": com_veredito[-1]["periodo"] if com_veredito else None,
        })

    indice = {"gerado": datetime.now().isoformat(timespec="seconds"), "raiz": str(raiz),
              "relatorios": relatorios, "empresas": empresas}
    _CACHE.update(assinatura=ass, indice=indice)
    return indice


def empresas_por_ticker(indice: dict, ticker: str) -> list[dict]:
    """Todas as pastas de empresa que correspondem ao ticker (normalmente uma; a carteira e a observação primeiro)."""
    t = ticker.upper()
    ordem = {"carteira": 0, "analise": 1, "varios": 2}
    es = [e for e in indice["empresas"] if t in (e["ticker"].upper(), e["ticker_yahoo"].upper(), e["nome"].upper())
          and e["categoria"] != "duplicado"]
    return sorted(es, key=lambda e: ordem.get(e["categoria"], 3))


def empresa_por_ticker(indice: dict, ticker: str) -> dict | None:
    es = empresas_por_ticker(indice, ticker)
    return es[0] if es else None


def relatorios_da_empresa(indice: dict, empresa: dict | None) -> list[dict]:
    if not empresa:
        return []
    por_id = {r["id"]: r for r in indice["relatorios"]}
    return [por_id[i] for i in empresa["relatorios"] if i in por_id]


# ---------------------------------------------------------------------------
# Conhecimento extraído: o que os teus relatórios dizem, pronto a usar na decisão
# ---------------------------------------------------------------------------
def conhecimento(indice: dict, ticker: str) -> dict:
    empresa = empresa_por_ticker(indice, ticker)
    rels = [r for r in relatorios_da_empresa(indice, empresa) if not r["arquivado"]]

    mais_recente = max(rels, key=_ordem)["id"] if rels else None

    def ultimo(campo):
        for r in sorted(rels, key=_ordem, reverse=True):
            meta = {"relatorio": r["id"], "titulo": r["titulo"], "data": r["data"], "periodo": r["periodo"],
                    "do_ultimo": r["id"] == mais_recente, "formato": r.get("formato", "antigo")}
            if r.get(campo):
                # do_ultimo = False: o relatório mais recente não traz este dado e vem de um anterior (pode estar desatualizado)
                d = r[campo] if isinstance(r[campo], dict) else {}
                return {"valor": d["valor"] if d else r[campo],
                        "excerto": d.get("excerto") if d else r.get("veredito_excerto"),
                        "origem": d.get("origem") or ("ficha" if r.get("formato") == "ficha" else "texto"), "moeda": d.get("moeda"),
                        "original": d.get("original"), "convertido": d.get("convertido", False), **meta}
            if r.get("formato") == "ficha":
                # A Ficha é a fonte de verdade: se o relatório com Ficha não traz o campo, é lacuna (não se recua)
                return {"valor": None, "lacuna": True, "excerto": None, "origem": "ficha", **meta}
        return None

    rec = ultimo("recomendacao")
    if rec:
        rec["nivel"] = nivel_recomendacao(rec["valor"])
        # Palavra fora das cinco (REFORÇAR, REDUZIR OU SAIR, NEUTRO...) num relatório antigo: mostra-se tal como está
        rec["fora_da_escala"] = bool(rec["valor"]) and rec["valor"] not in FI.RECOMENDACOES
    recente = max(rels, key=_ordem) if rels else {}
    return {
        "empresa": empresa,
        "n_relatorios": len(rels),
        "formato": recente.get("formato"),
        "ficha": {**(recente.get("ficha") or {}), "relatorio": recente.get("id"), "data": recente.get("data")} if rels else None,
        "baseline": {**(recente.get("baseline") or {}), "relatorio": recente.get("id"), "data": recente.get("data")} if rels else None,
        # Linhas «Filtros:» / «Cenários:» de todos os relatórios, do mais recente para o mais antigo (para recuar ao
        # relatório anterior com «Cenários:» válido quando o mais recente não os traz)
        "baselines": [{**(r.get("baseline") or {}), "relatorio": r["id"], "titulo": r["titulo"], "data": r["data"],
                       "do_ultimo": r["id"] == mais_recente} for r in sorted(rels, key=_ordem, reverse=True)],
        "tese": ultimo("veredito"),
        "recomendacao": rec,
        "preco_alvo": ultimo("preco_alvo"),
        "valor_intrinseco": ultimo("valor_intrinseco"),
        "fosso": ultimo("fosso"),
        "historico": [{"id": r["id"], "titulo": r["titulo"], "tipo": r["tipo"], "periodo": r["periodo"], "data": r["data"],
                       "veredito": r["veredito"], "recomendacao": r["recomendacao"],
                       "preco_alvo": (r["preco_alvo"] or {}).get("valor"), "fosso": (r["fosso"] or {}).get("valor"),
                       "fosso_original": (r["fosso"] or {}).get("original"), "formato": r.get("formato")}
                      for r in sorted(rels, key=_ordem)],
    }


def evidencias(indice: dict, ticker: str, termos: dict[str, list[str]], max_por_termo: int = 3) -> dict:
    """
    Para cada grupo de termos (ex.: {"custos_mudanca": ["custo de mudança", "switching cost"]}), conta as menções nos
    relatórios da empresa e devolve até `max_por_termo` excertos. Pesquisa sem acentos nem maiúsculas.
    """
    rels = [r for r in relatorios_da_empresa(indice, empresa_por_ticker(indice, ticker)) if not r["arquivado"]]
    saida = {}
    for grupo, lista in termos.items():
        total, excertos = 0, []
        for r in sorted(rels, key=_ordem, reverse=True):
            norm = normalizar(r["texto"])
            for termo in lista:
                t = normalizar(termo)
                i = norm.find(t)
                while i != -1:
                    total += 1
                    if len(excertos) < max_por_termo:
                        excertos.append({"excerto": _excerto(r["texto"], i, 80, 220), "relatorio": r["id"], "titulo": r["titulo"]})
                    i = norm.find(t, i + len(t))
        saida[grupo] = {"mencoes": total, "excertos": excertos}
    return saida


def _termos(q: str) -> list[str]:
    return [normalizar(a or b) for a, b in re.findall(r'"([^"]+)"|(\S+)', q or "") if (a or b).strip()]


_NORM: dict[str, tuple[str, str]] = {}  # id -> (texto normalizado do cabeçalho, do corpo)


def pesquisar(indice: dict, q: str, veredito: str | None = None, ocultos: bool = False, limite: int = 60) -> dict:
    """Todos os termos têm de aparecer (sem acentos nem maiúsculas); "entre aspas" = frase exata. Até 2 excertos por relatório."""
    termos = _termos(q)
    resultados = []
    for r in indice["relatorios"]:
        if r.get("erro") or (not ocultos and (r["arquivado"] or r["categoria"] == "duplicado")):
            continue
        if veredito and r.get("veredito") != veredito:
            continue
        if r["id"] not in _NORM:
            _NORM[r["id"]] = (normalizar(" ".join(str(x) for x in (r["ticker"], r["empresa"], r["titulo"], r["periodo"], r["ficheiro"]))),
                              normalizar(r["texto"]))
        cab, corpo = _NORM[r["id"]]
        pontos, excertos = 0, []
        for t in termos:
            no_cab, n = t in cab, corpo.count(t)
            if not no_cab and n == 0:
                break
            pontos += (50 if no_cab else 0) + min(n, 20)
            i = corpo.find(t)
            if i != -1 and len(excertos) < 2:
                excertos.append(_excerto(r["texto"], i, 90, 160))
        else:
            resultados.append({**{k: v for k, v in r.items() if k != "texto"}, "pontos": pontos, "excertos": excertos})
    resultados.sort(key=lambda x: (x["pontos"], x["data"]), reverse=True)  # mais relevante; em empate, mais recente
    return {"total": len(resultados), "termos": termos, "resultados": resultados[:limite]}


def indice_leve(indice: dict) -> dict:
    """O índice sem o texto integral dos relatórios (para a interface)."""
    return {**indice, "relatorios": [{k: v for k, v in r.items() if k != "texto"} for r in indice["relatorios"]]}


# ---------------------------------------------------------------------------
# Análises guardadas e notas (a parte da wiki escrita pela app)
# ---------------------------------------------------------------------------
def seguro(ticker: str) -> str:
    """Nome de ficheiro seguro para um ticker (ASML.AS fica ASML.AS; ^GSPC fica _GSPC)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker.upper())


_seguro = seguro


def guardar_analise(ticker: str, analise: dict) -> str:
    pasta = pasta_dados() / "analises" / _seguro(ticker)
    pasta.mkdir(parents=True, exist_ok=True)
    f = pasta / f"{date.today().isoformat()}.json"
    f.write_text(json.dumps(analise, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return f.name


def analises_guardadas(ticker: str) -> list[dict]:
    pasta = pasta_dados() / "analises" / _seguro(ticker)
    saida = []
    for f in sorted(pasta.glob("*.json"), reverse=True) if pasta.exists() else []:
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        saida.append({"data": f.stem, "nivel": a.get("decisao", {}).get("nivel"), "rotulo": a.get("decisao", {}).get("rotulo"),
                      "pontos": a.get("decisao", {}).get("pontos"), "preco": a.get("preco"), "analise": a})
    return saida


def ler_notas(ticker: str) -> dict:
    f = pasta_dados() / "notas" / f"{_seguro(ticker)}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def guardar_notas(ticker: str, notas: dict) -> dict:
    pasta = pasta_dados() / "notas"
    pasta.mkdir(parents=True, exist_ok=True)
    atual = ler_notas(ticker)
    atual.update(notas)
    atual["atualizado"] = datetime.now().isoformat(timespec="seconds")
    (pasta / f"{_seguro(ticker)}.json").write_text(json.dumps(atual, ensure_ascii=False, indent=2), encoding="utf-8")
    return atual
