"""
Onde estão as coisas (lido de config.json, na pasta do programa):

  pasta_relatorios — os teus relatórios HTML ("Estudos Empresas-Relatorios"), lidos diretamente;
  pasta_dados      — o que a app escreve: listas, critérios, análises guardadas, notícias, notas, traduções.
                     Fica no OneDrive (Documentos\\Decisão de Investimento\\Dados da app) para ter cópia de segurança.
"""
from __future__ import annotations

import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FICHEIRO = RAIZ / "config.json"


def _ler() -> dict:
    try:
        return json.loads(FICHEIRO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def pasta_relatorios() -> Path:
    p = _ler().get("pasta_relatorios")
    return Path(p) if p else Path.home() / "OneDrive" / "Ambiente de Trabalho" / "Estudos Empresas-Relatorios"


def pasta_dados() -> Path:
    p = _ler().get("pasta_dados")
    pasta = Path(p) if p else RAIZ / "dados"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def ler_json(nome: str, omissao):
    """Lê um ficheiro JSON da pasta de dados; se não existir ou estiver corrompido, devolve `omissao`."""
    try:
        return json.loads((pasta_dados() / nome).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return omissao


def escrever_json(nome: str, valor) -> None:
    f = pasta_dados() / nome
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f.suffix + ".tmp")
    tmp.write_text(json.dumps(valor, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(f)  # escrita atómica: um corte a meio não deixa o ficheiro estragado
