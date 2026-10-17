"""
Ligação aos relatórios do Rui na pasta "Estudos Empresas-Relatorios".

Procura os ficheiros <TICKER>_iniciacao_*.html e <TICKER>_FYAAAA-Qn_(analise|acompanhamento).html
e devolve-os do mais recente para o mais antigo (acompanhamento > análise > iniciação).
"""

from __future__ import annotations

import re
from pathlib import Path

def _pasta_raiz() -> Path:
    """A pasta dos relatórios é a primeira acima desta que tem o CLAUDE.md (a app pode mudar de sítio)."""
    aqui = Path(__file__).resolve().parent
    for p in aqui.parents:
        if (p / "CLAUDE.md").exists():
            return p
    return aqui.parent


PASTA_RAIZ = _pasta_raiz()
IGNORAR = {"APP", "_ARQUIVO", "_REVER_DUPLICADOS", "Claude outputs", "app-investimentos", "triagem_acoes",
           "_TEMPLATES", "_FERRAMENTAS", "node_modules", ".git"}

# tickers do Yahoo cujo prefixo nos relatórios é diferente
ALIASES = {"V": "VISA", "NOVO-B.CO": "NVO", "BONEX.ST": "BONEX", "ABBN.SW": "ABB", "REL.L": "RELX", "RELX.AS": "RELX",
           "GOOG": "GOOGL", "CNR.TO": "CNI"}

_PADRAO = re.compile(r"^([A-Z0-9]+)_(?:iniciacao_(\d{4}-\d{2}-\d{2})|FY(\d{4})-([QH])(\d)_(analise|acompanhamento))",
                     re.IGNORECASE)
_TIPO = {"acompanhamento": 2, "analise": 1, None: 0}


def _prefixo(ticker: str) -> str:
    t = ticker.upper()
    return ALIASES.get(t, t.split(".")[0])


def procurar(ticker: str) -> list[dict]:
    prefixo = _prefixo(ticker)
    encontrados = []
    if not PASTA_RAIZ.exists():
        return encontrados
    for f in PASTA_RAIZ.rglob(f"{prefixo}_*.htm*"):
        if any(parte in IGNORAR for parte in f.relative_to(PASTA_RAIZ).parts):
            continue
        m = _PADRAO.match(f.name)
        if not m or m.group(1).upper() != prefixo:
            continue
        if m.group(2):  # iniciação
            chave = (int(m.group(2)[:4]), 0, 0)
            tipo = "Iniciação"
        else:
            ano, per, n = int(m.group(3)), m.group(4).upper(), int(m.group(5))
            chave = (ano, n * 3 if per == "Q" else n * 6, _TIPO[m.group(6).lower()])
            tipo = f"FY{ano}-{per}{n} {m.group(6).lower()}"
        encontrados.append({"ficheiro": f, "tipo": tipo, "chave": chave})
    return sorted(encontrados, key=lambda x: x["chave"], reverse=True)
