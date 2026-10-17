"""
Empresas enviadas pela Pesquisa do Centro de Investimento para analisar, sem entrar em nenhuma lista.

A app analisa-as em segundo plano (a mesma análise completa da página da empresa) e mostra-as na barra lateral em
«Da Pesquisa · por decidir». O Rui decide na página da empresa: «+ Carteira», «+ Em observação» ou «Dispensar».
Quando uma entra numa lista (ou é dispensada), sai daqui. Ficheiro: `por_decidir.json` na pasta de dados.
"""
from __future__ import annotations

import threading
from datetime import datetime

import config

FICHEIRO = "por_decidir.json"
_tranca = threading.Lock()


def ler() -> list[dict]:
    v = config.ler_json(FICHEIRO, [])
    return v if isinstance(v, list) else []


def _gravar(lista: list[dict]) -> None:
    config.escrever_json(FICHEIRO, lista)


def juntar(empresas: list[dict], origem: str, ja_nas_listas: set[str]) -> dict:
    """Acrescenta as que ainda não estão aqui nem numa lista. Devolve as novas e as que ficaram de fora (e porquê)."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    novas, fora = [], []
    with _tranca:
        lista = ler()
        presentes = {x["ticker"] for x in lista}
        for e in empresas:
            t = str(e.get("ticker") or "").strip().upper()
            if not t:
                continue
            if t in ja_nas_listas:
                fora.append({"ticker": t, "motivo": "já está na carteira ou em observação"})
            elif t in presentes:
                fora.append({"ticker": t, "motivo": "já estava por decidir"})
            else:
                lista.append({"ticker": t, "nome": e.get("nome") or t, "origem": origem, "enviada": agora, "estado": "a_analisar"})
                presentes.add(t)
                novas.append(t)
        _gravar(lista)
    return {"novas": novas, "fora": fora}


def marcar(ticker: str, **campos) -> None:
    with _tranca:
        lista = ler()
        for x in lista:
            if x["ticker"] == ticker:
                x.update(campos)
        _gravar(lista)


def retirar(ticker: str) -> bool:
    with _tranca:
        lista = ler()
        resto = [x for x in lista if x["ticker"] != ticker.strip().upper()]
        if len(resto) == len(lista):
            return False
        _gravar(resto)
        return True
