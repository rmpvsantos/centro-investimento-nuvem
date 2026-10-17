"""
Regras configuráveis por ti (ficheiro regras.json na pasta de dados):

  criterios_desativados — critérios da triagem que não contam (ex.: "pe_atual" para ignorar o P/E face ao grupo);
  personalizados        — critérios teus, sobre qualquer métrica da análise fundamental
                          (ex.: margem bruta >= 40%, SBC em % do FCF <= 15%);
  pilares_desativados   — pilares da decisão que não contam (ex.: "rsi" para ignorar o RSI);
  valor_ancora          — que valor conta no pilar «Preço face ao valor»: "relatorio" (por omissão: o valor ponderado da
                          linha «Cenários:» do relatório mais recente; com relatório mas sem essa linha, o pilar fica
                          «não verificável»; sem nenhum relatório, conta o valor de referência da app) ou "app" (o valor
                          de referência da app, que inclui preços-alvo de analistas).

Os limiares dos critérios da triagem (ROIC mínimo, dívida líquida / EBITDA, ...) continuam em criterios.json.
"""
from __future__ import annotations

import math
import re

import config
import fundamental as F

FICHEIRO = "regras.json"
OPERADORES = {">=": "≥", "<=": "≤"}

# Critérios da triagem (chave do motor -> rótulo curto)
CRITERIOS_TRIAGEM = {
    "receita_cagr": "Receitas crescentes",
    "eps_var_total": "EPS crescente",
    "fcf_cagr": "FCF crescente",
    "ebitda_cagr": "EBITDA crescente",
    "roic_ultimo": "ROIC mínimo",
    "roce_ultimo": "ROCE mínimo",
    "nd_ebitda_atual": "Dívida líquida / EBITDA",
    "cobertura_atual": "Cobertura de juros",
    "pe_atual": "P/E face ao grupo",
}

PILARES = {
    "qualidade": "Qualidade do negócio (triagem)",
    "valor": "Preço face ao valor intrínseco",
    "tendencia": "Tendência (MM50 / MM200)",
    "rsi": "RSI 14",
    "gestao": "Gestão",
    "moat": "Moat",
    "tese": "Tese nos teus relatórios",
}

ANCORAS = {"app": "Valor de referência da app (inclui preços-alvo de analistas)",
           "relatorio": "Valor ponderado da linha «Cenários:» do relatório"}
ANCORA_OMISSAO = "relatorio"

# Métricas disponíveis para critérios teus: todas as da análise fundamental que não são valores absolutos
CATALOGO = {chave: {"rotulo": rotulo, "formato": fmt, "segmento": seg}
            for seg, linhas in F.SEGMENTOS for rotulo, chave, fmt, _melhor, comparavel in linhas if comparavel}


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and not math.isinf(v)


def ler() -> dict:
    r = config.ler_json(FICHEIRO, {})
    return {
        "criterios_desativados": [c for c in r.get("criterios_desativados", []) if c in CRITERIOS_TRIAGEM],
        "personalizados": [p for p in r.get("personalizados", []) if p.get("chave") in CATALOGO],
        "pilares_desativados": [p for p in r.get("pilares_desativados", []) if p in PILARES],
        "valor_ancora": r.get("valor_ancora") if r.get("valor_ancora") in ANCORAS else ANCORA_OMISSAO,
    }


def validar_personalizado(p: dict) -> dict:
    chave, op, valor = p.get("chave"), p.get("operador"), p.get("valor")
    if chave not in CATALOGO:
        raise ValueError(f"Métrica desconhecida: {chave}")
    if op not in OPERADORES:
        raise ValueError("O operador tem de ser >= ou <=")
    if not _ok(valor):
        raise ValueError("O valor tem de ser um número")
    ident = p.get("id") or re.sub(r"[^a-z0-9_]", "", f"{chave}_{'min' if op == '>=' else 'max'}_{valor}".lower().replace(".", "_"))
    return {"id": ident, "chave": chave, "operador": op, "valor": float(valor), "ativo": bool(p.get("ativo", True)),
            "eliminatorio": bool(p.get("eliminatorio", False))}


def guardar(novas: dict) -> dict:
    atual = ler()
    if "criterios_desativados" in novas:
        atual["criterios_desativados"] = [c for c in novas["criterios_desativados"] if c in CRITERIOS_TRIAGEM]
    if "pilares_desativados" in novas:
        atual["pilares_desativados"] = [p for p in novas["pilares_desativados"] if p in PILARES]
    if "valor_ancora" in novas:
        if novas["valor_ancora"] not in ANCORAS:
            raise ValueError("valor_ancora tem de ser 'app' ou 'relatorio'")
        atual["valor_ancora"] = novas["valor_ancora"]
    if "personalizados" in novas:
        vistos, lista = set(), []
        for p in novas["personalizados"]:
            v = validar_personalizado(p)
            if v["id"] not in vistos:
                vistos.add(v["id"])
                lista.append(v)
        atual["personalizados"] = lista
    config.escrever_json(FICHEIRO, atual)
    return atual


def _formatar(v: float, fmt: str) -> str:
    if not _ok(v):
        return "n/d"
    if fmt == "pct":
        return f"{v * 100:.1f}%".replace(".", ",")
    return f"{v:.1f}x".replace(".", ",") if fmt == "x" else f"{v:.2f}".replace(".", ",")


def nome_personalizado(p: dict) -> str:
    m = CATALOGO[p["chave"]]
    return f"{m['rotulo']} {OPERADORES[p['operador']]} {_formatar(p['valor'], m['formato'])}"


def avaliar_personalizados(personalizados: list[dict], valores: dict | None) -> list[dict]:
    """Critérios teus no mesmo formato dos da triagem (estado cumpre / falha / nd)."""
    saida = []
    for p in personalizados:
        if not p.get("ativo", True):
            continue
        m = CATALOGO[p["chave"]]
        v = (valores or {}).get(p["chave"])
        if not _ok(v):
            estado, detalhe = "nd", "sem dados para esta métrica" if valores else "análise fundamental indisponível"
        else:
            ok = v >= p["valor"] if p["operador"] == ">=" else v <= p["valor"]
            estado = "cumpre" if ok else "falha"
            detalhe = f"{_formatar(v, m['formato'])} face ao exigido {OPERADORES[p['operador']]} {_formatar(p['valor'], m['formato'])}"
        saida.append({"criterio": nome_personalizado(p), "estado": estado, "detalhe": detalhe, "chave": f"pessoal:{p['id']}",
                      "valor": v if _ok(v) else None, "tipo": m["formato"], "limiar": f"{OPERADORES[p['operador']]} {_formatar(p['valor'], m['formato'])}",
                      "melhor": "maior" if p["operador"] == ">=" else "menor", "personalizado": True, "metrica": p["chave"],
                      "eliminatorio": p.get("eliminatorio", False)})
    return saida


def aplicar(criterios: list[dict], valores: dict | None, regras: dict | None = None) -> list[dict]:
    """Tira os critérios desativados e junta os teus."""
    regras = regras or ler()
    fora = set(regras["criterios_desativados"])
    base = [dict(c, desativado=False) for c in criterios if c.get("chave") not in fora]
    return base + avaliar_personalizados(regras["personalizados"], valores)
