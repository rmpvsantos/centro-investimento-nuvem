"""
Limiares dos critérios de triagem (ROIC, ROCE, dívida líquida / EBITDA, cobertura de juros, P/E face ao grupo).
Guardados em criterios.json, ao lado da aplicação, para ficarem alterados entre sessões. Os quatro critérios de
tendência (receita, EPS, FCF e EBITDA crescentes) não têm número: exigem sempre "subir em todos os anos".
"""
from __future__ import annotations

import json
from pathlib import Path

FICHEIRO = Path(__file__).resolve().parents[2] / "dados" / "criterios.json"  # o servidor aponta-o para a pasta de dados

# chave -> (rótulo, tipo "pct" ou "x", valor de origem)
DEFINICAO = {
    "roic": ("ROIC mínimo (último exercício)", "pct", 0.15),
    "roce": ("ROCE mínimo (último exercício)", "pct", 0.15),
    "nd_ebitda": ("Dívida líquida / EBITDA: cumpre sempre abaixo de", "x", 0.5),
    "cobertura": ("Cobertura de juros: cumpre acima de", "x", 5.0),
    "pe_cumpre": ("P/E face à mediana do grupo: CUMPRE até este desvio acima", "pct", 0.10),
    "pe_parcial": ("P/E face à mediana do grupo: PARCIAL até este desvio acima (acima disto, FALHA)", "pct", 0.30),
}
OMISSAO = {k: v[2] for k, v in DEFINICAO.items()}


def carregar() -> dict:
    """Lê o ficheiro de configuração; se não existir, estiver corrompido, ou tiver um valor inválido, usa a origem."""
    if not FICHEIRO.exists():
        return dict(OMISSAO)
    try:
        guardado = json.loads(FICHEIRO.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(OMISSAO)
    if not isinstance(guardado, dict):
        return dict(OMISSAO)
    out = dict(OMISSAO)
    for k in OMISSAO:
        v = guardado.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0:
            out[k] = float(v)
    return out


def guardar(valores: dict) -> dict:
    """Atualiza só as chaves passadas (as outras mantêm o valor guardado) e devolve a configuração final."""
    atuais = carregar()
    for k, v in valores.items():
        if k in OMISSAO and isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0:
            atuais[k] = float(v)
    FICHEIRO.parent.mkdir(parents=True, exist_ok=True)
    FICHEIRO.write_text(json.dumps(atuais, indent=2, ensure_ascii=False), encoding="utf-8")
    return atuais


def repor() -> dict:
    """Apaga o ficheiro de configuração: os critérios voltam aos valores de origem."""
    if FICHEIRO.exists():
        FICHEIRO.unlink()
    return dict(OMISSAO)


def alterado(valores: dict) -> bool:
    return any(abs(valores.get(k, OMISSAO[k]) - OMISSAO[k]) > 1e-9 for k in OMISSAO)
