"""
Avaliação qualitativa: o que os números não mostram.

Cada pergunta vale 0 (não), 1 (em parte) ou 2 (sim). As respostas ficam gravadas
em qualitativo/<TICKER>.json, com a data, para se poder rever mais tarde.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

PASTA = Path(__file__).parent / "qualitativo"

PERGUNTAS = [
    ("negocio", "Percebo o negócio",
     "Sei explicar em duas frases como a empresa ganha dinheiro e porque é que os clientes lhe pagam."),
    ("vantagem", "Vantagem competitiva identificável",
     "Marca, custos de mudança, efeito de rede, vantagem de custo/escala, patentes ou licenças. Consigo nomear qual."),
    ("vantagem_trend", "A vantagem está a alargar-se",
     "Quota de mercado a subir, capacidade de subir preços sem perder clientes, concorrentes a ficar para trás."),
    ("mercado", "Mercado com crescimento estrutural",
     "O mercado cresce por razões de fundo (demografia, digitalização, regulação), não por um ciclo passageiro."),
    ("gestao", "Gestão competente e honesta",
     "Cumpre o que promete, comunica os erros com franqueza, não muda de métricas quando lhe convém."),
    ("alinhamento", "Gestão alinhada com os acionistas",
     "Fundadores ou gestores com participação relevante; remuneração ligada a ROIC ou FCF por ação, não só a receitas."),
    ("capital", "Boa alocação de capital",
     "Aquisições que criaram valor, recompras a preços razoáveis, dividendos sustentáveis, sem aventuras."),
    ("dependencia", "Sem dependências perigosas",
     "Nenhum cliente, fornecedor, produto ou decisão regulatória única pode pôr o negócio em causa."),
    ("disrupcao", "Resistente à disrupção a 10 anos",
     "A tecnologia ou um novo concorrente não tornam o produto irrelevante na próxima década."),
    ("recessao", "Resiliente numa recessão",
     "Produto essencial ou receitas recorrentes; em 2008-09 e 2020 as receitas aguentaram razoavelmente."),
]

OPCOES = {0: "Não", 1: "Em parte", 2: "Sim"}


def _ficheiro(ticker: str) -> Path:
    return PASTA / f"{ticker.upper().replace('/', '_')}.json"


def carregar(ticker: str) -> dict:
    f = _ficheiro(ticker)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def gravar(ticker: str, respostas: dict, notas: str, pre_mortem: str) -> dict:
    PASTA.mkdir(exist_ok=True)
    registo = {"ticker": ticker.upper(), "data": date.today().isoformat(), "respostas": respostas,
               "notas": notas, "pre_mortem": pre_mortem}
    _ficheiro(ticker).write_text(json.dumps(registo, ensure_ascii=False, indent=2), encoding="utf-8")
    return registo


def pontuacao(registo: dict) -> float | None:
    """0 a 100; None se ainda não foi preenchido."""
    resp = {k: v for k, v in (registo.get("respostas") or {}).items() if v is not None}
    if not resp:
        return None
    return 100 * sum(resp.values()) / (2 * len(PERGUNTAS))


def pontos_fracos(registo: dict) -> list[str]:
    resp = registo.get("respostas") or {}
    return [titulo for chave, titulo, _ in PERGUNTAS if resp.get(chave) == 0]
