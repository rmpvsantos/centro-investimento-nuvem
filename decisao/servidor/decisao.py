"""
Decisão por regras: junta seis pilares num só nível — COMPRAR, ACUMULAR, MANTER / AGUARDAR, REDUZIR, VENDER.

  1. Qualidade do negócio   — sugestão da triagem quantitativa (sim / com reservas / não)
  2. Preço face ao valor    — desconto do preço atual face ao valor que conta (o valor ponderado da linha «Cenários:» do
                              relatório; sem relatório, o valor de referência da app)
  3. Técnica                — tendência (MM50 / MM200) e RSI 14
  4. Gestão                 — nota da grelha qualitativa (0 a 10)
  5. Moat                   — largo / estreito / nenhum
  6. Tese (wiki)            — último veredito dos teus relatórios, se existir

Cada pilar dá pontos; a soma define o nível e os travões impedem sinais de compra quando falta informação essencial ou há
um sinal vermelho. Todos os cortes estão aqui em cima. É a aplicação mecânica destas regras, não uma recomendação.
"""
from __future__ import annotations

import math

PONTOS_QUALIDADE = {"sim": 1, "reservas": 0, "nao": -2}
# Desconto = 1 − preço / valor (positivo = preço abaixo do valor), a mesma convenção de regras_cowork.py.
FAIXAS_VALOR = (  # (desconto mínimo, pontos, leitura)
    (0.15, 2, "pelo menos 15% abaixo do valor"),
    (0.00, 1, "abaixo do valor"),
    (-0.15, 0, "até 15% acima do valor"),
    (-0.35, -1, "entre 15% e 35% acima do valor"),
    (-math.inf, -2, "mais de 35% acima do valor"),
)
PONTOS_TENDENCIA = {"subida": 1, "lateral": 0, "descida": -1}
RSI_SOBREVENDA, RSI_SOBRECOMPRA = 30, 70
CORTES_GESTAO = ((7.0, 1, "forte"), (4.0, 0, "adequada"), (-math.inf, -1, "fraca"))
PONTOS_MOAT = {"largo": 1, "estreito": 0, "nenhum": -1}
PONTOS_TESE = {"reforcada": 1, "intacta": 0, "enfraquecida": -1, "quebrada": -2}

NIVEIS = (  # (pontos mínimos, chave, rótulo)
    (4, "comprar", "COMPRAR"),
    (2, "acumular", "ACUMULAR"),
    (-1, "manter", "MANTER / AGUARDAR"),
    (-3, "reduzir", "REDUZIR"),
    (-math.inf, "vender", "VENDER"),
)
ORDEM = [n[1] for n in NIVEIS]
EPS = 1e-9


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and not math.isinf(v)


def _pct(x: float) -> str:
    return f"{'+' if x > 0 else ''}{x * 100:.1f}%".replace(".", ",")


def texto_desconto(d: float) -> str:
    """«29,1% abaixo» / «12,0% acima» (desconto positivo = preço abaixo do valor)."""
    return f"{abs(d) * 100:.1f}% {'abaixo' if d >= 0 else 'acima'}".replace(".", ",")


def _teto(nivel: str, teto: str) -> str:
    return teto if ORDEM.index(nivel) < ORDEM.index(teto) else nivel


def calcular(qualidade: str | None = None, preco: float | None = None, mediana_valor: float | None = None,
             tendencia: str = "nd", rsi: float = float("nan"), nota_gestao: float | None = None,
             moat: str | None = None, tese: str | None = None, moat_tendencia: str | None = None,
             desativados: set | list | None = None, rotulo_valor: str = "do valor",
             leitura_sem_valor: str = "sem valor intrínseco de referência") -> dict:
    """mediana_valor: o valor que conta no pilar «Preço face ao valor» (nome antigo mantido); rotulo_valor diz qual é
    («do valor ponderado do relatório (…)»); leitura_sem_valor: o que se mostra quando não há valor que conte.
    desativados: pilares que não contam (qualidade, valor, tendencia, rsi, gestao, moat, tese); os travões desses
    pilares também deixam de se aplicar. moat_tendencia: "a reforçar" | "estável" | "a erodir" (a erodir tira 1 ponto)."""
    fora = set(desativados or [])
    pilares, total = [], 0

    def pilar(nome, leitura, pontos, chave):
        nonlocal total
        if chave in fora:
            pilares.append({"pilar": nome, "chave": chave, "leitura": "desativado por ti", "pontos": None, "desativado": True})
            return
        pilares.append({"pilar": nome, "chave": chave, "leitura": leitura, "pontos": pontos})
        if pontos is not None:
            total += pontos

    # 1. Qualidade
    if qualidade in PONTOS_QUALIDADE:
        pilar("Qualidade do negócio", {"sim": "passa a triagem", "reservas": "passa a triagem com reservas",
                                        "nao": "não passa a triagem"}[qualidade], PONTOS_QUALIDADE[qualidade], "qualidade")
    else:
        pilar("Qualidade do negócio", "sem triagem", None, "qualidade")

    # 2. Valor (desconto = 1 − preço / valor; positivo = preço abaixo do valor)
    desconto = None
    if _ok(mediana_valor) and mediana_valor > 0 and _ok(preco) and preco > 0:
        desconto = 1 - preco / mediana_valor
        _, p, txt = next(f for f in FAIXAS_VALOR if desconto >= f[0] - EPS)
        pilar("Preço face ao valor", f"preço {texto_desconto(desconto)} {rotulo_valor}: {txt}", p, "valor")
    else:
        pilar("Preço face ao valor", leitura_sem_valor, None, "valor")

    # 3. Técnica
    if tendencia in PONTOS_TENDENCIA:
        pilar("Tendência", {"subida": "subida (preço e MM50 acima da MM200)", "descida": "descida (preço e MM50 abaixo da MM200)",
                            "lateral": "indefinida (sinais mistos face à MM200)"}[tendencia], PONTOS_TENDENCIA[tendencia], "tendencia")
    else:
        pilar("Tendência", "histórico insuficiente para a MM200", None, "tendencia")
    if _ok(rsi):
        p = 1 if rsi <= RSI_SOBREVENDA else -1 if rsi >= RSI_SOBRECOMPRA else 0
        pilar("RSI (14)", f"{rsi:.1f} — {'sobrevenda' if p == 1 else 'sobrecompra' if p == -1 else 'zona neutra'}".replace(".", ","), p, "rsi")
    else:
        pilar("RSI (14)", "sem dados", None, "rsi")

    # 4. Gestão
    classe_gestao = None
    if _ok(nota_gestao):
        _, p, classe_gestao = next(c for c in CORTES_GESTAO if nota_gestao >= c[0])
        pilar("Gestão", f"nota {nota_gestao:.1f} em 10 — {classe_gestao}".replace(".", ","), p, "gestao")
    else:
        pilar("Gestão", "sem avaliação", None, "gestao")

    # 5. Moat (um moat a erodir vale menos 1 ponto)
    if moat in PONTOS_MOAT:
        p = PONTOS_MOAT[moat] - (1 if moat_tendencia == "a erodir" else 0)
        leitura = {"largo": "largo", "estreito": "estreito", "nenhum": "sem moat evidente"}[moat]
        if moat_tendencia:
            leitura += f", {moat_tendencia}" + (" (−1)" if moat_tendencia == "a erodir" else "")
        pilar("Moat", leitura, p, "moat")
    else:
        pilar("Moat", "sem avaliação", None, "moat")

    # 6. Tese (wiki)
    if tese in PONTOS_TESE:
        pilar("Tese nos teus relatórios", {"reforcada": "reforçada", "intacta": "intacta", "enfraquecida": "enfraquecida",
                                           "quebrada": "quebrada"}[tese], PONTOS_TESE[tese], "tese")
    else:
        pilar("Tese nos teus relatórios", "sem acompanhamento trimestral", None, "tese")

    nivel = next(c for m, c, _ in NIVEIS if total >= m)
    travoes = []

    def travar(teto, motivo):
        nonlocal nivel
        novo = _teto(nivel, teto)
        if novo != nivel:
            travoes.append(motivo)
        nivel = novo

    if desconto is None and "valor" not in fora:
        travar("acumular", "Sem valor que conte no pilar «Preço face ao valor», o sinal não passa de ACUMULAR.")
    if qualidade == "nao" and "qualidade" not in fora:
        travar("manter", "O negócio não passa a triagem: o sinal não passa de MANTER / AGUARDAR.")
    if desconto is not None and desconto < FAIXAS_VALOR[3][0] - EPS and "valor" not in fora:
        travar("manter", "Preço mais de 35% acima do valor: o sinal não passa de MANTER / AGUARDAR.")
    if classe_gestao == "fraca" and "gestao" not in fora:
        travar("manter", "Gestão avaliada como fraca: o sinal não passa de MANTER / AGUARDAR.")
    if tese == "quebrada" and "tese" not in fora:
        travar("reduzir", "A tese está quebrada nos teus relatórios: o sinal não passa de REDUZIR.")

    avaliados = [p for p in pilares if p["pontos"] is not None]
    return {
        "nivel": nivel,
        "rotulo": next(r for _, c, r in NIVEIS if c == nivel),
        "pontos": total,
        "pilares": pilares,
        "travoes": travoes,
        "n_avaliados": len(avaliados),
        "n_pilares": len([p for p in pilares if not p.get("desativado")]),
        "desconto_valor": desconto,
    }


# ---------------------------------------------------------------------------
# Alinhamento com as regras do Cowork (pedido do Rui a 11/10/2026)
# ---------------------------------------------------------------------------
NIVEL_COWORK = {"COMPRAR": "comprar", "ACUMULAR": "acumular", "MANTER": "manter", "REDUZIR": "reduzir", "VENDER": "vender"}


def alinhar_cowork(d: dict, verificacao: dict | None) -> dict:
    """
    O sinal final fala a língua dos relatórios:
      - regras do Cowork verificadas (relatório com «Cenários:» válido e preço atual): o nível é o veredicto delas
        (filtros, desconto ≥ 20% ao ponderado, assimetria ≥ 2, tese); os pontos dos pilares ficam só como informação;
      - não verificáveis (sem relatório, sem «Cenários:», moeda diferente...): nunca COMPRAR nem ACUMULAR, porque sem os
        cenários não se verifica a assimetria nem os filtros. Fica no máximo MANTER, com o motivo. REDUZIR e VENDER pelos
        pontos ficam (são avisos de risco, não pedem confirmação).
    Guarda sempre o nível dos pontos em `nivel_pontos` / `rotulo_pontos` e a origem do nível em `origem_nivel`.
    """
    d = dict(d, travoes=list(d.get("travoes") or []))
    d["nivel_pontos"], d["rotulo_pontos"] = d["nivel"], d["rotulo"]
    rotulo = {c: r for _, c, r in NIVEIS}
    v = (verificacao or {}).get("veredicto")
    if (verificacao or {}).get("estado") == "verificado" and v and v.get("nivel") in NIVEL_COWORK:
        novo = NIVEL_COWORK[v["nivel"]]
        d["origem_nivel"] = "cowork"
        d["cowork"] = {"nivel": v["nivel"], "mostrar": v.get("mostrar") or v["nivel"], "motivo": v.get("motivo"),
                       "confirmado": v.get("confirmado", True), "datas": verificacao.get("datas")}
        nota = "" if v.get("confirmado", True) else " (não confirmado)"
        if novo != d["nivel"]:
            d["travoes"].append(f"Regras do Cowork ({verificacao.get('datas')}): {d['cowork']['mostrar']}{nota}, porque "
                                f"{v.get('motivo')}. Pelos pontos da app seria {d['rotulo_pontos']}.")
        d["nivel"], d["rotulo"] = novo, rotulo[novo]
        return d
    d["origem_nivel"] = "pontos"
    if d["nivel"] in ("comprar", "acumular"):
        motivo = (verificacao or {}).get("motivo") or "sem relatório"
        d["travoes"].append(f"As regras do Cowork não se verificam ({motivo}): sem os cenários do relatório não há COMPRAR "
                            f"nem ACUMULAR. Pelos pontos da app seria {d['rotulo_pontos']}; para confirmar, pede o relatório "
                            f"ao Cowork.")
        d["nivel"], d["rotulo"] = "manter", rotulo["manter"]
    return d


def regras() -> dict:
    """As regras em forma de dados, para a interface as mostrar sempre atualizadas."""
    return {
        "qualidade": PONTOS_QUALIDADE,
        "valor": [{"desconto_desde": None if math.isinf(a) else a, "pontos": p, "leitura": t} for a, p, t in FAIXAS_VALOR],
        "tendencia": PONTOS_TENDENCIA,
        "rsi": {"sobrevenda": RSI_SOBREVENDA, "sobrecompra": RSI_SOBRECOMPRA},
        "gestao": [{"desde": None if math.isinf(c) else c, "pontos": p, "classe": k} for c, p, k in CORTES_GESTAO],
        "moat": PONTOS_MOAT,
        "tese": PONTOS_TESE,
        "niveis": [{"minimo": None if math.isinf(m) else m, "chave": c, "rotulo": r} for m, c, r in NIVEIS],
    }
