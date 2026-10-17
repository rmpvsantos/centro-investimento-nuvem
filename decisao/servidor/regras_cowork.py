"""
Regras de decisão do Cowork (resumo no CLAUDE.md da pasta de relatórios), num único módulo.

Servem para VERIFICAR, não para decidir: a decisão oficial é a Recomendação da Ficha de dados do relatório. Com dados
suficientes (linhas «Filtros:» e «Cenários:» do baseline e o preço atual da app), este módulo calcula o veredicto pelas
regras e compara-o com a Ficha. Se divergirem, devolve o aviso «divergência: o relatório diz X, as regras dizem Y». Não
corrige nada. Falta de dados = «não verificável», nunca uma estimativa.

Cada filtro tem um estado: passa, falha, não verificável (campo em falta ou em lacuna) ou, só na regra de ROIC,
«parcial: tendência não verificável» (nível e WACC cumprem, mas a linha «Filtros:» não traz o campo opcional
«tendência 3a»). Nunca «passa» sem verificação. Com um filtro não verificável, as regras não dão COMPRAR nem ACUMULAR.
O ROIC parcial não bloqueia nem gera divergência sozinho, mas um COMPRAR / ACUMULAR fica «não confirmado».

Desconto = 1 − preço / valor (positivo = preço abaixo do valor): a única convenção da app.
Todos os limiares estão na tabela LIMIARES (calibração sem backtest). Não os repetir noutros sítios.
"""
from __future__ import annotations

import math

LIMIARES = {
    "desconto_compra": 0.20,   # preço pelo menos 20% abaixo do valor ponderado para COMPRAR / ACUMULAR
    "premio_reduzir": 0.10,    # preço mais de 10% acima do valor ponderado: REDUZIR (com posição)
    "assimetria_min": 2.0,     # (ganho até ao base) / (perda até ao bear) mínima para COMPRAR / ACUMULAR
    "fosso_min": 6.0,          # em 10
    "gestao_min": 3.0,         # em 5
    "governacao_min": 3.0,     # em 5
    "roic_min": 0.10,          # ROIC dos últimos 12 meses
}
RESULTADOS_ACEITES = ("merecem confiança", "merecem confiança com reservas")
TOLERANCIA_PROB = 0.005        # as probabilidades têm de somar 100% (±0,5 pontos)
PASSA, FALHA, POR_VERIFICAR, PARCIAL = "passa", "falha", "nao_verificavel", "parcial"
TENDENCIAS_ROIC_OK = ("ascendente", "estável")  # «não descendente»; «descendente» falha


def _pct(x: float, casas: int = 1) -> str:
    return f"{x * 100:.{casas}f}%".replace(".", ",")


def _n(x: float, casas: int = 2) -> str:
    return f"{x:.{casas}f}".replace(".", ",")


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v) and not math.isinf(v)


# ---------------------------------------------------------------------------
# Aritmética dos cenários
# ---------------------------------------------------------------------------
def calcular_cenarios(bear: dict, base: dict, bull: dict, preco: float) -> dict:
    """
    Valor ponderado = soma de probabilidade × valor (as probabilidades têm de somar 100%: senão, erro visível).
    Desconto ao ponderado = 1 − preço / ponderado (positivo = preço abaixo do ponderado).
    Assimetria = (ganho até ao base) / (perda até ao bear) = (base − preço) / (preço − bear). Se o preço está acima do
    bear, está ACIMA: a distância ao bear é perda potencial, não margem de segurança. Preço igual ou abaixo do bear:
    não há perda até ao bear e a assimetria não se calcula (fica None, com nota).
    Preços de entrada (não dependem do preço atual):
      - com assimetria mínima A: o preço em que (base − p) / (p − bear) = A, ou seja p = (base + A × bear) / (1 + A)
        (com A = 2: (base + 2 × bear) / 3);
      - com o desconto mínimo: ponderado × (1 − 20%);
      - de compra pelas regras: o menor dos dois (tem de cumprir as duas condições).
    """
    soma = bear["prob"] + base["prob"] + bull["prob"]
    if abs(soma - 1) > TOLERANCIA_PROB:
        return {"erro": f"as probabilidades somam {_pct(soma)} e não 100%", "soma_prob": soma}
    ponderado = bear["prob"] * bear["valor"] + base["prob"] * base["valor"] + bull["prob"] * bull["valor"]
    a = LIMIARES["assimetria_min"]
    entrada_assimetria = (base["valor"] + a * bear["valor"]) / (1 + a)
    entrada_desconto = ponderado * (1 - LIMIARES["desconto_compra"])
    entradas = {"assimetria": entrada_assimetria, "desconto": entrada_desconto,
                "compra": min(entrada_assimetria, entrada_desconto)}
    if not _ok(preco):
        return {"ponderado": ponderado, "desconto": None, "assimetria": None, "ganho_ate_base": None,
                "perda_ate_bear": None, "nota": "sem preço", "soma_prob": soma, "entradas": entradas}
    desconto = 1 - preco / ponderado
    perda, ganho = preco - bear["valor"], base["valor"] - preco
    assimetria, nota = (ganho / perda, None) if perda > 0 else (None, "preço igual ou abaixo do bear: sem perda até ao bear")
    return {"ponderado": ponderado, "desconto": desconto, "assimetria": assimetria, "ganho_ate_base": ganho,
            "perda_ate_bear": perda, "nota": nota, "soma_prob": soma, "entradas": entradas}


def cenarios_validos(c: dict | None) -> str | None:
    """None se a linha «Cenários:» serve para decidir; senão, o motivo (para «não verificável»)."""
    c = c or {}
    if not all(c.get(k) for k in ("bear", "base", "bull")):
        return "o relatório não traz cenários (bear, base e bull)"
    if not c.get("moeda"):
        return "a linha «Cenários:» não indica a moeda"
    soma = sum(c[k]["prob"] for k in ("bear", "base", "bull"))
    if abs(soma - 1) > TOLERANCIA_PROB:
        return f"as probabilidades dos cenários somam {_pct(soma)} e não 100%"
    return None


# ---------------------------------------------------------------------------
# Filtros: passa / falha / não verificável
# ---------------------------------------------------------------------------
def avaliar_filtros(f: dict | None) -> dict:
    """Cada filtro com o estado, o valor, o limiar e o motivo. A média das notas é só informação, nunca critério."""
    L, f = LIMIARES, f or {}

    def item(nome, valor, regra, estado, nota=None):
        return {"filtro": nome, "valor": valor, "regra": regra, "estado": estado, "passa": estado == PASSA, "nota": nota}

    def nota(nome, chave, minimo, escala):
        v = f.get(chave)
        regra = f"≥ {_n(minimo, 0)}/{escala}"
        if not _ok(v):
            return item(nome, "lacuna", regra, POR_VERIFICAR, "sem valor na linha «Filtros:»")
        return item(nome, f"{_n(v, 1)}/{escala}", regra, PASSA if v >= minimo else FALHA)

    lista = [nota("Fosso", "fosso", L["fosso_min"], 10),
             nota("Gestão", "gestao", L["gestao_min"], 5),
             nota("Estrutura acionista e governação", "governacao", L["governacao_min"], 5)]

    res = f.get("resultados")
    if not res:
        lista.append(item("Qualidade dos resultados", "lacuna", "merecem confiança ou com reservas", POR_VERIFICAR,
                          "sem valor na linha «Filtros:»"))
    else:
        lista.append(item("Qualidade dos resultados", res, "merecem confiança ou com reservas",
                          PASSA if res in RESULTADOS_ACEITES else FALHA,
                          "com reservas: peso-alvo reduzido" if res == "merecem confiança com reservas" else None))

    roic, wacc = (f.get("roic") or {}).get("ttm"), f.get("wacc")
    regra_roic = f"ROIC ≥ {_pct(L['roic_min'], 0)}, acima do WACC e tendência a 3 anos não descendente"
    media = (f.get("roic") or {}).get("media_5a")
    ref5 = f"; média a 5 anos {_pct(media)} (só referência)" if _ok(media) else ""
    if not (_ok(roic) and _ok(wacc)):
        lista.append(item("Regra de ROIC", "lacuna", regra_roic, POR_VERIFICAR, "falta o ROIC ou o WACC na linha «Filtros:»"))
    else:
        valor = f"ROIC {_pct(roic)} · WACC {_pct(wacc)} · spread {_n((roic - wacc) * 100, 1)} pontos percentuais"
        tend = (f.get("roic") or {}).get("tendencia_3a")
        if roic < L["roic_min"] or roic <= wacc:
            lista.append(item("Regra de ROIC", valor, regra_roic, FALHA, f"ROIC abaixo do mínimo ou do WACC{ref5}"))
        elif tend is None:
            lista.append(item("Regra de ROIC", valor, regra_roic, PARCIAL,
                              f"parcial: tendência não verificável (nível e WACC cumprem; a linha «Filtros:» não traz "
                              f"«tendência 3a»){ref5}"))
        elif tend in TENDENCIAS_ROIC_OK:
            lista.append(item("Regra de ROIC", valor, regra_roic, PASSA, f"tendência a 3 anos {tend}{ref5}"))
        else:
            lista.append(item("Regra de ROIC", valor, regra_roic, FALHA, f"tendência a 3 anos {tend}{ref5}"))

    falhados = [x["filtro"] for x in lista if x["estado"] == FALHA]
    por_verificar = [x["filtro"] for x in lista if x["estado"] == POR_VERIFICAR]
    parciais = [x["filtro"] for x in lista if x["estado"] == PARCIAL]
    return {"lista": lista, "passam": not falhados and not por_verificar and not parciais, "falhados": falhados,
            "por_verificar": por_verificar, "parciais": parciais}


# ---------------------------------------------------------------------------
# Veredicto pelo preço
# ---------------------------------------------------------------------------
def veredicto(filtros: dict, cen: dict, com_posicao: bool, tese: str | None = None) -> dict:
    """
    nivel: o que a Ficha deveria dizer (COMPRAR, ACUMULAR, MANTER, REDUZIR, VENDER).
    mostrar: o que a app mostra (igual ao nível, ou «NÃO COMPRAR» quando não há posição nem condições de compra).
    caso: identificador da linha da tabela usada (para os testes e para mostrar o motivo).
    nivel_se_verificado: com filtros por verificar, o nível que as regras dariam se passassem (para não acusar
    divergência quando a Ficha diz isso mesmo).
    """
    L = LIMIARES
    d, a = cen["desconto"], cen["assimetria"]
    sem = not com_posicao
    pendentes = filtros.get("por_verificar") or []

    parciais = filtros.get("parciais") or []

    def r(nivel, caso, motivo, mostrar=None, se_verificado=None, confirmado=True):
        return {"nivel": nivel, "mostrar": mostrar or nivel, "caso": caso, "motivo": motivo,
                "nivel_se_verificado": se_verificado or nivel, "confirmado": confirmado}

    if filtros["falhados"] or tese == "quebrada":
        motivo = "tese quebrada" if tese == "quebrada" else f"filtro falhado: {', '.join(filtros['falhados'])}"
        return r("MANTER", "filtro_ou_tese", motivo, "NÃO COMPRAR") if sem else r("VENDER", "filtro_ou_tese", motivo)

    if d >= L["desconto_compra"] - 1e-12:
        if a is None or a >= L["assimetria_min"] - 1e-12:
            txt = f"preço {_pct(d)} abaixo do ponderado e assimetria {'sem perda até ao bear' if a is None else _n(a)}"
            compra = "COMPRAR" if sem else "ACUMULAR"
            if pendentes:  # nunca uma compra com filtros por verificar
                return r("MANTER", "filtros_por_verificar", f"{txt}, mas por verificar: {', '.join(pendentes)}",
                         "NÃO COMPRAR" if sem else None, se_verificado=compra)
            if parciais:  # ROIC parcial: o nível fica, mas não confirmado
                return r(compra, "compra", f"{txt}; não confirmado: tendência do ROIC a 3 anos não verificável",
                         confirmado=False)
            return r(compra, "compra", txt)
        # Caso que a tabela das skills não prevê: desconto suficiente mas assimetria abaixo de 2. Leitura mais exigente:
        # não há COMPRAR nem ACUMULAR; fica MANTER (e, sem posição, «NÃO COMPRAR»), com o motivo à vista.
        motivo = f"assimetria {_n(a)} abaixo de {_n(L['assimetria_min'], 0)} (preço {_pct(d)} abaixo do ponderado)"
        return r("MANTER", "desconto_sem_assimetria", motivo, "NÃO COMPRAR" if sem else None)

    if d >= -L["premio_reduzir"] - 1e-12:
        motivo = f"preço {_pct(abs(d))} {'abaixo' if d >= 0 else 'acima'} do ponderado: menos de {_pct(L['desconto_compra'], 0)} de desconto"
        return r("MANTER", "zona_manter", motivo, "NÃO COMPRAR" if sem else None)

    motivo = f"preço {_pct(-d)} acima do ponderado (mais de {_pct(L['premio_reduzir'], 0)})"
    return r("MANTER", "acima_sem_posicao", motivo, "NÃO COMPRAR") if sem else r("REDUZIR", "reduzir", motivo)


# ---------------------------------------------------------------------------
# Datas
# ---------------------------------------------------------------------------
def _data_pt(s: str) -> str:
    """«2026-10-10» -> «10/10/2026»; «2026-10-10 21:59» -> «10/10/2026 21:59»."""
    d, _, resto = str(s).partition(" ")
    partes = d.split("-")
    return ("/".join(reversed(partes)) if len(partes) == 3 else d) + (f" {resto}" if resto else "")


def texto_datas(data_cenarios: str | None, data_preco: str | None, hoje: str | None = None) -> str:
    """«cenários de 10/10/2026, preço de hoje às 20:59» (ou «preço de 09/10/2026 às 20:59», ou sem hora)."""
    partes = [f"cenários de {_data_pt(data_cenarios)}" if data_cenarios else "cenários sem data"]
    if data_preco:
        dia, _, hora = str(data_preco).partition(" ")
        quando = "hoje" if hoje and dia == hoje else _data_pt(dia)
        partes.append(f"preço de {quando}" + (f" às {hora}" if hora else ""))
    else:
        partes.append("preço sem data")
    return ", ".join(partes)


# ---------------------------------------------------------------------------
# Verificação da Ficha
# ---------------------------------------------------------------------------
def verificar(baseline: dict | None, recomendacao_ficha: str | None, preco: float | None, moeda_preco: str | None,
              com_posicao: bool, tese: str | None = None, data_relatorio: str | None = None,
              data_preco: str | None = None, hoje: str | None = None) -> dict:
    """
    Recalcula o veredicto pelas regras com o preço atual da app e compara com a Recomendação da Ficha.
    Precisa da linha «Cenários:» completa (bear, base, bull, moeda) e do preço; os filtros em falta ficam «não
    verificável» um a um (não impedem o resto da verificação).
    """
    datas = texto_datas(data_relatorio, data_preco, hoje)
    base = {"data_relatorio": data_relatorio, "data_preco": data_preco, "datas": datas, "limiares": LIMIARES}
    faltam = list((baseline or {}).get("faltam") or [])
    estado_b = (baseline or {}).get("estado", "ausente")
    if estado_b == "nao_reconhecido":
        return {**base, "estado": "nao_verificavel", "motivo": "formato de baseline não reconhecido", "faltam": faltam}
    c, f = (baseline or {}).get("cenarios"), (baseline or {}).get("filtros")
    motivo_c = cenarios_validos(c)
    if motivo_c:
        return {**base, "estado": "nao_verificavel", "faltam": faltam or ["linha Cenários"], "motivo": motivo_c}
    if not (isinstance(preco, (int, float)) and preco > 0 and not math.isnan(preco)):
        return {**base, "estado": "nao_verificavel", "faltam": ["preço atual"], "motivo": "sem preço atual"}
    if moeda_preco and moeda_preco != c["moeda"]:
        return {**base, "estado": "nao_verificavel", "faltam": [],
                "motivo": f"moeda do preço ({moeda_preco}) diferente da dos cenários ({c['moeda']})"}

    cen = calcular_cenarios(c["bear"], c["base"], c["bull"], preco)
    filtros = avaliar_filtros(f)
    v = veredicto(filtros, cen, com_posicao, tese)

    # Coerência das contas do próprio relatório (com o preço do relatório, se ele o escrever)
    preco_rel = (c.get("preco") or {}).get("valor")
    no_rel = calcular_cenarios(c["bear"], c["base"], c["bull"], preco_rel) if _ok(preco_rel) else None
    contas = []
    if no_rel:
        for chave, escrito, calc, tol, fmt in (
                ("ponderado", (c.get("ponderado") or {}).get("valor"), no_rel["ponderado"], 0.006, lambda x: _n(x)),
                ("desconto ao ponderado", c.get("desconto"), no_rel["desconto"], 0.0006, _pct),
                ("assimetria", c.get("assimetria"), no_rel["assimetria"], 0.006, lambda x: _n(x))):
            if calc is not None and escrito is not None and abs(escrito - calc) > tol:
                contas.append(f"o relatório escreve {chave} {fmt(escrito)}, as contas dão {fmt(calc)}")

    rec = (recomendacao_ficha or "").upper() or None
    # Com filtros por verificar, não é divergência se a Ficha diz o que as regras diriam com eles a passar
    divergente = bool(rec) and rec != v["nivel"] and rec != v["nivel_se_verificado"]
    aviso = f"divergência: o relatório diz {rec}, as regras dizem {v['nivel']} ({datas})"
    pendente = None
    if rec and rec != v["nivel"] and rec == v["nivel_se_verificado"]:
        pendente = (f"o relatório diz {rec}; as regras só o confirmam com {', '.join(filtros['por_verificar'])} "
                    f"verificado(s) — não verificável pela app ({datas})")
    return {**base, "estado": "verificado", "cenarios": cen, "no_relatorio": no_rel, "filtros": filtros, "veredicto": v,
            "recomendacao_ficha": rec, "divergente": divergente, "avisos_contas": contas, "com_posicao": com_posicao,
            "aviso": aviso if divergente else None, "pendente": pendente, "faltam": faltam}
