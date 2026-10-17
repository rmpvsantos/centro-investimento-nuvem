"""
Veredito: junta qualidade, preço, alertas e avaliação qualitativa.

As notas de qualidade e de preço ficam separadas de propósito: um número único
esconderia se a empresa é boa e cara ou má e barata, que é o que interessa saber.
"""

from __future__ import annotations

LIMIAR_QUALIDADE = 70
LIMIAR_QUALIDADE_FRACA = 50
LIMIAR_PRECO = 60
LIMIAR_QUALITATIVO = 60
CONFIANCA_MINIMA = 0.6

CLASSES = {
    "candidato": ("Bom negócio a preço atrativo", "Aprofundar: passa nos filtros de qualidade e de preço."),
    "espera": ("Bom negócio, mas caro", "Lista de espera: acompanhar e esperar pelo preço de entrada."),
    "armadilha": ("Barato, mas com problemas", "Possível armadilha de valor: o desconto pode ter razão de ser."),
    "neutro": ("Sem interesse para já", "Nem suficientemente bom, nem suficientemente barato."),
    "incompleto": ("Dados insuficientes", "Falta informação para uma conclusão: confirmar nos relatórios da empresa."),
}

ORDEM = ["candidato", "espera", "armadilha", "neutro", "incompleto"]


def veredito(qual: dict, val: dict, pont_qualitativa: float | None = None) -> dict:
    q, p = qual.get("pontuacao"), val.get("pontuacao")
    graves = [a for a in qual.get("alertas", []) if a["gravidade"] == "grave"]
    razoes = []

    if q is None or p is None or qual.get("confianca", 0) < CONFIANCA_MINIMA or val.get("confianca", 0) < CONFIANCA_MINIMA:
        classe = "incompleto"
        if val.get("erro"):
            razoes.append(f"Avaliação: {val['erro']}.")
    elif q >= LIMIAR_QUALIDADE and p >= LIMIAR_PRECO and not graves:
        classe = "candidato"
    elif q >= LIMIAR_QUALIDADE and not graves:
        classe = "espera"
    elif p >= LIMIAR_PRECO and (q < LIMIAR_QUALIDADE_FRACA or graves):
        classe = "armadilha"
    else:
        classe = "neutro"

    if val.get("divergencia"):
        razoes.append("O crescimento histórico e a previsão dos analistas divergem mais de 10 p.p.: o negócio está a "
                      "mudar e o valor intrínseco é pouco fiável. Lê os relatórios antes de confiar na nota de preço.")
    if graves:
        razoes.append(f"{len(graves)} alerta(s) grave(s): " + " ".join(a["alerta"] for a in graves))
    if q is not None:
        razoes.append(f"Qualidade {q:.0f}/100 (limiar {LIMIAR_QUALIDADE}).")
    if p is not None:
        razoes.append(f"Preço {p:.0f}/100 (limiar {LIMIAR_PRECO}).")

    # a avaliação qualitativa pode travar um candidato, nunca promover uma empresa que falha nos números
    if pont_qualitativa is None:
        if classe == "candidato":
            razoes.append("Falta a avaliação qualitativa: sem ela, isto é apenas um bom perfil numérico.")
    elif pont_qualitativa < LIMIAR_QUALITATIVO and classe == "candidato":
        classe = "neutro"
        razoes.append(f"A avaliação qualitativa ({pont_qualitativa:.0f}/100) fica abaixo de {LIMIAR_QUALITATIVO}: "
                      "os números são bons mas o negócio não convence.")
    else:
        razoes.append(f"Avaliação qualitativa {pont_qualitativa:.0f}/100.")

    titulo, acao = CLASSES[classe]
    return {"classe": classe, "titulo": titulo, "acao": acao, "razoes": razoes,
            "confianca": min(qual.get("confianca", 0), val.get("confianca", 0))}
