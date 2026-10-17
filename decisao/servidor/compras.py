"""
As tuas compras e vendas de uma ação, para o gráfico da Técnica (botão «Compras»).

Fonte: a cópia da Carteira de Ativos que a Carteira no Google grava no PC (`carteira_dados.json`, na pasta do Google Drive
«Carteira - sincronização»). Só se lê; nunca se escreve.

A folha usa os tickers do Google Finance (LON:REL, ETR:NOV) e o gráfico os do Yahoo (REL.L, NOV.DE): um movimento só
entra no gráfico da mesma cotação. Os preços ficam na moeda da folha; a única conversão é pence → libras (÷ 100), marcada.
Noutra moeda (ex.: compras em REL.L e gráfico do ADR RELX em USD) não se converte nada: avisa-se onde estão as compras.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

EXPORTACAO = Path(os.environ.get("CARTEIRA_EXPORTACAO") or r"G:\O meu disco\Carteira - sincronização\carteira_dados.json")

# Prefixo do Google Finance -> sufixo do Yahoo (o mesmo mapa da sincronização da Carteira)
GOOGLE_YAHOO = {"STO:": ".ST", "LON:": ".L", "ETR:": ".DE", "AMS:": ".AS", "EPA:": ".PA", "CPH:": ".CO", "SWX:": ".SW",
                "BME:": ".MC", "BIT:": ".MI", "ELI:": ".LS", "OSL:": ".OL", "HEL:": ".HE", "TSE:": ".TO", "EBR:": ".BR"}
# Mesma empresa noutra cotação (relatórios e Decisão usam o ADR; a folha a bolsa de origem)
OUTRA_COTACAO = {"RELX": "LON:REL", "NVO": "ETR:NOV"}
PENCE = {"GBX", "GBp", "GBP_PENCE"}


def yahoo_da_folha(t: str) -> str:
    """LON:REL -> REL.L; ETR:NOV -> NOV.DE; MSFT -> MSFT (a mesma cotação, sem trocar pelo ADR)."""
    t = (t or "").strip().upper()
    if ":" in t:
        pref, base = t.split(":", 1)
        return base.replace(".", "-") + GOOGLE_YAHOO.get(pref + ":", "")
    return t


def ler_carteira() -> dict | None:
    try:
        return json.loads(EXPORTACAO.read_text(encoding="utf-8")) or None
    except (OSError, json.JSONDecodeError):
        return None


def compras_para_grafico(exportacao: dict | None, ticker: str, moeda_grafico: str | None) -> dict:
    t = ticker.strip().upper()
    if not exportacao or not (exportacao.get("dados") or {}).get("movimentos"):
        return {"ok": False, "motivo": "Ainda não há cópia da Carteira no PC (abre a Carteira no Google uma vez).", "compras": []}
    d = exportacao["dados"]
    origem = f"Carteira de Ativos (cópia de {exportacao.get('exportado') or '?'})"
    moedas = {c.get("ticker"): c.get("moeda") for c in d.get("cotacoes") or []}
    da_folha = sorted({m["ticker"] for m in d["movimentos"] if m.get("ticker") and yahoo_da_folha(m["ticker"]) == t})
    if not da_folha:
        outra = OUTRA_COTACAO.get(t)
        if outra and any(m.get("ticker") == outra and m.get("tipo") in ("Compra", "Venda") for m in d["movimentos"]):
            return {"ok": False, "compras": [], "origem": origem, "abrir": yahoo_da_folha(outra),
                    "motivo": f"As tuas compras estão registadas em {outra}, noutra moeda: vê-as no gráfico de {yahoo_da_folha(outra)}."}
        return {"ok": False, "compras": [], "origem": origem, "motivo": "Sem compras desta ação na Carteira."}
    folha = da_folha[0]
    moeda_folha = moedas.get(folha)
    fator, nota = None, ""
    if moeda_folha and moeda_grafico and moeda_folha.upper() == moeda_grafico.upper():
        fator = 1.0
    elif moeda_folha in PENCE and (moeda_grafico or "").upper() == "GBP":
        fator, nota = 0.01, "preços da folha em pence, convertidos em libras (÷ 100)"
    if fator is None:
        return {"ok": False, "compras": [], "origem": origem,
                "motivo": f"As compras de {folha} estão em {moeda_folha or 'moeda desconhecida'} e o gráfico em {moeda_grafico or '?'}: "
                          "não se convertem moedas."}
    movs = sorted((m for m in d["movimentos"] if m.get("ticker") in da_folha and m.get("tipo") in ("Compra", "Venda")
                   and isinstance(m.get("preco"), (int, float)) and isinstance(m.get("qtd"), (int, float)) and m.get("data")),
                  key=lambda m: (m["data"], m.get("linha") or 0))
    lista = [{"data": m["data"][:10], "tipo": m["tipo"], "qtd": m["qtd"], "preco": m["preco"] * fator, "preco_folha": m["preco"]} for m in movs]
    # Lotes ainda em carteira (FIFO, como a Carteira) e o preço médio deles na moeda do gráfico, sem comissões nem câmbio
    lotes: list[list[float]] = []
    for m in lista:
        if m["tipo"] == "Compra":
            lotes.append([m["qtd"], m["preco"]])
        else:
            falta = m["qtd"]
            while falta > 1e-9 and lotes:
                usar = min(falta, lotes[0][0])
                lotes[0][0] -= usar
                falta -= usar
                if lotes[0][0] <= 1e-9:
                    lotes.pop(0)
    qtd = sum(q for q, _ in lotes)
    medio = sum(q * p for q, p in lotes) / qtd if qtd > 1e-9 else None
    return {"ok": True, "ticker_folha": folha, "moeda": moeda_grafico, "nota": nota, "origem": origem, "compras": lista,
            "qtd_em_carteira": qtd if qtd > 1e-9 else 0, "preco_medio": medio}
