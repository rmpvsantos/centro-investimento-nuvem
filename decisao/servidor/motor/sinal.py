"""
Sinal por regras (sem Streamlit): junta três pilares num só nível — COMPRAR, ACUMULAR, MANTER / AGUARDAR, REDUZIR, VENDER.

  1. Qualidade do negócio: a sugestão da triagem individual (sim / com reservas / não).
  2. Preço face ao valor: cotação comparada com a mediana do valor intrínseco de referência e com o preço de entrada base.
  3. Técnica: tendência (médias de 50 e 200 sessões) e RSI de 14 sessões.

Cada pilar dá pontos; a soma define o nível e há travões que impedem sinais de compra quando falta informação ou o negócio
não passa na triagem. Os cortes são arbitrários e estão todos aqui em cima para serem fáceis de ler e de alterar.
É a aplicação mecânica destas regras aos dados do Yahoo, não uma recomendação de investimento.
"""
from __future__ import annotations

import math

# --- Pilar 1: qualidade (nível da triagem individual) ---------------------------------------------------------------
PONTOS_QUALIDADE = {"sim": 1, "reservas": 0, "nao": -2}

# --- Pilar 2: preço face à mediana do valor de referência (cotação / mediana − 1) ------------------------------------
# A primeira faixa usa o preço de entrada base (mediana − 15%). Lê-se: "até X acima da mediana, vale Y pontos".
FAIXAS_VALOR = (
    (-0.15, 2, "abaixo do preço de entrada base (mediana − 15%)"),
    (0.00, 1, "abaixo da mediana do valor de referência"),
    (0.15, 0, "até 15% acima da mediana"),
    (0.35, -1, "entre 15% e 35% acima da mediana"),
    (math.inf, -2, "mais de 35% acima da mediana"),
)

# --- Pilar 3: técnica -------------------------------------------------------------------------------------------------
PONTOS_TENDENCIA = {"subida": 1, "lateral": 0, "descida": -1}
RSI_SOBREVENDA, RSI_SOBRECOMPRA = 30, 70

# --- Da soma ao nível -------------------------------------------------------------------------------------------------
NIVEIS = (  # (pontos mínimos, chave, rótulo)
    (3, "comprar", "COMPRAR"),
    (2, "acumular", "ACUMULAR"),
    (-1, "manter", "MANTER / AGUARDAR"),
    (-2, "reduzir", "REDUZIR"),
    (-math.inf, "vender", "VENDER"),
)
ORDEM = [n[1] for n in NIVEIS]  # do mais positivo para o mais negativo
EPS = 1e-9  # os cortes de valor incluem o próprio limite (135/100 − 1 dá 0,35000000000000001)


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and v == v and not math.isinf(v)


def _pct(x: float) -> str:
    return f"{'+' if x > 0 else ''}{x * 100:.1f}%".replace(".", ",")


def _nivel(pontos: float) -> str:
    return next(chave for minimo, chave, _ in NIVEIS if pontos >= minimo)


def _no_maximo(nivel: str, teto: str) -> str:
    """Limita o nível a `teto` (não deixa ser mais positivo do que ele)."""
    return teto if ORDEM.index(nivel) < ORDEM.index(teto) else nivel


def calcular(qualidade: str | None, preco: float, mediana_valor: float | None, tendencia: str, rsi: float) -> dict:
    """
    qualidade: "sim" | "reservas" | "nao" | None (sem triagem);
    mediana_valor: mediana do valor intrínseco de referência, ou None se não houver;
    tendencia: "subida" | "lateral" | "descida" | "nd";  rsi: último RSI (NaN se não houver).
    """
    pilares, travoes, total = [], [], 0

    # 1. Qualidade
    if qualidade in PONTOS_QUALIDADE:
        p = PONTOS_QUALIDADE[qualidade]
        txt = {"sim": "passa a triagem", "reservas": "passa a triagem com reservas", "nao": "não passa a triagem"}[qualidade]
        pilares.append({"pilar": "Qualidade do negócio", "leitura": txt, "pontos": p})
        total += p
    else:
        pilares.append({"pilar": "Qualidade do negócio", "leitura": "sem triagem", "pontos": None})

    # 2. Valor
    face = None
    if _ok(mediana_valor) and mediana_valor > 0 and _ok(preco) and preco > 0:
        face = preco / mediana_valor - 1
        limite, p, txt = next(f for f in FAIXAS_VALOR if face <= f[0] + EPS)
        pilares.append({"pilar": "Preço face ao valor", "leitura": f"cotação {_pct(face)} face à mediana: {txt}", "pontos": p})
        total += p
    else:
        pilares.append({"pilar": "Preço face ao valor", "leitura": "sem valor intrínseco de referência", "pontos": None})

    # 3. Técnica
    if tendencia in PONTOS_TENDENCIA:
        p = PONTOS_TENDENCIA[tendencia]
        txt = {"subida": "tendência de subida (preço e MM50 acima da MM200)",
               "descida": "tendência de descida (preço e MM50 abaixo da MM200)",
               "lateral": "tendência indefinida (sinais mistos face à MM200)"}[tendencia]
        pilares.append({"pilar": "Tendência", "leitura": txt, "pontos": p})
        total += p
    else:
        pilares.append({"pilar": "Tendência", "leitura": "histórico insuficiente para a MM200", "pontos": None})

    if _ok(rsi):
        p = 1 if rsi <= RSI_SOBREVENDA else -1 if rsi >= RSI_SOBRECOMPRA else 0
        zona = "sobrevenda" if p == 1 else "sobrecompra" if p == -1 else "zona neutra"
        pilares.append({"pilar": "RSI (14)", "leitura": f"{rsi:.1f} — {zona}".replace(".", ","), "pontos": p})
        total += p
    else:
        pilares.append({"pilar": "RSI (14)", "leitura": "sem dados", "pontos": None})

    nivel = _nivel(total)

    # Travões: não há sinal de compra sem avaliação, com negócio fraco ou com preço muito esticado
    if face is None:
        novo = _no_maximo(nivel, "acumular")
        if novo != nivel:
            travoes.append("Sem valor intrínseco de referência o sinal não passa de ACUMULAR.")
        nivel = novo
    if qualidade == "nao":
        novo = _no_maximo(nivel, "manter")
        if novo != nivel:
            travoes.append("O negócio não passa a triagem: o sinal não passa de MANTER / AGUARDAR.")
        nivel = novo
    if face is not None and face > FAIXAS_VALOR[3][0] + EPS:
        novo = _no_maximo(nivel, "manter")
        if novo != nivel:
            travoes.append("Cotação mais de 35% acima da mediana do valor: o sinal não passa de MANTER / AGUARDAR.")
        nivel = novo

    avaliados = [p for p in pilares if p["pontos"] is not None]
    return {
        "nivel": nivel,
        "rotulo": next(r for _, c, r in NIVEIS if c == nivel),
        "pontos": total,
        "pilares": pilares,
        "travoes": travoes,
        "completo": len(avaliados) == len(pilares),
        "face_valor": face,
    }
