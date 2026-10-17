"""
A tua carteira, a lista em observação e os «outros» (ficheiro listas.json na pasta de dados).

As listas seguem as pastas dos relatórios:
  - empresas com pasta em CARTEIRA INVESTIMENTOS  -> carteira
  - empresas com pasta em EM OBSERVAÇÃO           -> em observação
  - empresas com pasta em ANALISES ANTIGAS        -> outros (fora das duas listas)

Mudar uma empresa na app (botões «Mover para…» / «Retirar») move a pasta dela para o contentor certo: nada é apagado,
é só uma mudança de sítio, e os relatórios vão juntos. Mover a pasta à mão no Explorador de Ficheiros tem o mesmo efeito.

Empresas sem pasta de relatórios (ainda sem nenhum relatório teu) ficam só registadas no listas.json.
"""
from __future__ import annotations

import shutil
from datetime import date

import config
import wiki

LISTAS = {"carteira": "Carteira", "observacao": "Em observação"}
DESTINOS = {**LISTAS, "outros": "Outros"}
CONTENTOR = {"carteira": "CARTEIRA INVESTIMENTOS", "observacao": "EM OBSERVAÇÃO", "outros": "ANALISES ANTIGAS"}
FICHEIRO = "listas.json"
CATEGORIA_PARA_LISTA = {"carteira": "carteira", "analise": "observacao"}


def _vazio() -> dict:
    return {k: [] for k in LISTAS}


def pelas_pastas(indice: dict) -> dict[str, dict]:
    """ticker -> {"lista", "nome", "pasta"} para as empresas com pasta na carteira ou na observação."""
    saida = {}
    for e in indice.get("empresas", []):
        lista = CATEGORIA_PARA_LISTA.get(e["categoria"])
        if not lista or e["ticker_yahoo"] in saida:
            continue
        rels = wiki.relatorios_da_empresa(indice, e)
        pasta = wiki.pasta_da_empresa(rels[0]["caminho"]).replace("/", "\\") if rels else e["nome"]
        saida[e["ticker_yahoo"]] = {"lista": lista, "nome": e["nome"], "pasta": pasta}
    return saida


def sincronizar(atual: dict, indice: dict) -> dict:
    """Listas = empresas das pastas + as que acrescentaste na app sem pasta numa das duas (ficam onde as puseste)."""
    anteriores = {x["ticker"].upper(): x for k in LISTAS for x in atual.get(k, [])}
    forcadas = pelas_pastas(indice)
    novas = _vazio()
    for t, f in forcadas.items():
        antes = anteriores.get(t.upper(), {})
        novas[f["lista"]].append({"ticker": t, "nome": f["nome"], "desde": antes.get("desde") or date.today().isoformat(),
                                  "origem": "pasta", "pasta": f["pasta"]})
    for k in LISTAS:
        for x in atual.get(k, []):
            if x.get("origem") == "app" and x["ticker"].upper() not in {t.upper() for t in forcadas}:
                novas[k].append(x)
    for k in LISTAS:
        novas[k].sort(key=lambda x: x["ticker"])
    return novas


def ler(indice: dict | None = None) -> dict:
    atual = config.ler_json(FICHEIRO, None) or _vazio()
    for k in LISTAS:
        atual.setdefault(k, [])
    if indice is None:
        return atual
    novas = sincronizar(atual, indice)
    if novas != {k: atual[k] for k in LISTAS}:
        config.escrever_json(FICHEIRO, novas)
    return novas


def onde_esta(listas: dict, ticker: str) -> str | None:
    t = ticker.upper()
    return next((k for k in LISTAS if any(x["ticker"].upper() == t for x in listas[k])), None)


def mover(ticker: str, destino: str, nome: str | None = None, indice: dict | None = None) -> dict:
    """
    Põe a empresa na carteira, em observação ou nos outros. Se tiver pasta de relatórios, move a pasta para o contentor
    desse destino (sem apagar nem substituir nada); se não tiver, regista-a (ou tira-a) no listas.json.
    Devolve {"listas", "movida": {"de", "para"} | None, "mensagem"}.
    """
    if destino not in DESTINOS:
        raise ValueError(f"Destino desconhecido: {destino}")
    t = ticker.upper()
    indice = indice or wiki.construir_indice()
    raiz = wiki.pasta_relatorios()
    empresa = wiki.empresa_por_ticker(indice, t)
    rels = wiki.relatorios_da_empresa(indice, empresa)
    movida = None

    if rels:  # tem pasta de relatórios: muda-se a pasta de contentor
        relativa = wiki.pasta_da_empresa(rels[0]["caminho"])
        origem = raiz / relativa
        contentor = raiz / CONTENTOR[destino]
        if not contentor.is_dir():
            raise ValueError(f"Não encontro a pasta «{CONTENTOR[destino]}» dentro de {raiz}.")
        alvo = contentor / origem.name
        if origem.resolve() != alvo.resolve():
            if alvo.exists():
                raise ValueError(f"Já existe uma pasta «{origem.name}» em {CONTENTOR[destino]}: não mexi em nada.")
            shutil.move(str(origem), str(alvo))
            movida = {"de": relativa.replace("/", "\\"), "para": f"{CONTENTOR[destino]}\\{origem.name}"}
            indice = wiki.construir_indice()  # as pastas mudaram: lê de novo

    atual = config.ler_json(FICHEIRO, None) or _vazio()
    for k in LISTAS:
        atual[k] = [x for x in atual.get(k, []) if x["ticker"].upper() != t]
    if not rels and destino in LISTAS:  # sem pasta: fica registada na lista
        atual[destino].append({"ticker": t, "nome": nome or t, "desde": date.today().isoformat(), "origem": "app"})
    config.escrever_json(FICHEIRO, atual)
    listas = ler(indice)
    if movida:
        mensagem = f"{t}: pasta movida de {movida['de']} para {movida['para']}."
    elif destino == "outros":
        mensagem = f"{t} retirada da carteira e da observação."
    else:
        mensagem = f"{t} está em {DESTINOS[destino].lower()}."
    return {"listas": listas, "movida": movida, "mensagem": mensagem}


def adicionar(lista: str, ticker: str, nome: str | None = None, indice: dict | None = None) -> dict:
    return mover(ticker, lista, nome, indice)["listas"]


def retirar(ticker: str, indice: dict | None = None) -> dict:
    return mover(ticker, "outros", None, indice)["listas"]


def seguidas(listas: dict) -> list[str]:
    """Todas as empresas que acompanhas (carteira primeiro)."""
    return [x["ticker"] for k in LISTAS for x in listas[k]]
