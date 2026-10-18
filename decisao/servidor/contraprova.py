"""
Contraprova do relatório pelo DCF invertido (pedido do Rui a 18/10/2026).

Pergunta: que crescimento anual do fluxo de caixa livre (FCF, já sem a remuneração em ações) é preciso, durante ANOS
anos, para o DCF dar
  - o preço atual do Yahoo (o que o mercado assume);
  - o valor do cenário base do relatório e o valor ponderado (o que o relatório assume, traduzido na mesma régua).
Tudo com os mesmos dados (FCF, dívida líquida e ações do Yahoo), a mesma taxa (o WACC da linha «Filtros:» do relatório
ou, sem ela, o WACC estimado pela app) e o mesmo crescimento a perpetuidade. Compara-se ainda com o crescimento
histórico do FCF e da receita.

Não muda o sinal nem a decisão: serve para ver onde está a aposta do relatório. O DCF do Cowork tem os seus próprios
pressupostos (margens, investimento, anos), por isso o «crescimento equivalente» é uma tradução na régua da app, não o
número escrito no relatório. Nos bancos e seguradoras o FCF não mede o negócio: não se calcula.
Funções puras (sem rede): testadas em testes/teste_contraprova.py.
"""
from __future__ import annotations

import math

ANOS = 10                 # anos de crescimento explícito antes da perpetuidade (o habitual num DCF invertido)
DIFERENCA_RELEVANTE = 0.03  # 3 pontos percentuais por ano: abaixo disto, as duas leituras dizem o mesmo
HISTORICO_MAX = 0.50      # um crescimento histórico acima de 50% ao ano (ou abaixo de −50%) vem de um ano de partida
                          # quase nulo ou negativo: não serve de comparação


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v) and not math.isinf(v)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".", ",")


def _pp(x: float) -> str:
    return f"{abs(x) * 100:.1f}".replace(".", ",") + " pontos percentuais"


def calcular(apoio: dict, preco: float | None, dcf_invertido, pressupostos_dcf, wacc_relatorio: float | None = None,
             base: float | None = None, ponderado: float | None = None, tipo_setor: str | None = None) -> dict:
    """
    apoio: o «apoio» da análise fundamental (fcf_base, divida_liquida, acoes, wacc, cagr_fcf, cagr_receita, moeda_ok...).
    dcf_invertido / pressupostos_dcf: as funções do motor (passadas para este módulo não depender dele).
    base / ponderado: valores do relatório na moeda do preço (já confirmada pela verificação).
    """
    if tipo_setor in ("banco", "seguradora"):
        return {"ok": False, "motivo": "nos bancos e seguradoras o fluxo de caixa livre não mede o negócio"}
    if not _ok(preco) or preco <= 0:
        return {"ok": False, "motivo": "sem preço atual"}
    if _ok(wacc_relatorio) and wacc_relatorio > 0:
        taxa, origem_taxa = wacc_relatorio, "WACC da linha «Filtros:» do relatório"
    elif _ok(apoio.get("wacc")):
        taxa, origem_taxa = apoio["wacc"], "WACC estimado pela app (o relatório não traz o WACC)"
    else:
        return {"ok": False, "motivo": "sem taxa de desconto (nem WACC do relatório nem da app)"}
    gt = pressupostos_dcf(apoio)["crescimento_terminal"]

    def resolver(valor):
        r = dcf_invertido(dict(apoio, preco=valor), taxa, gt, ANOS)
        if not r.get("ok"):
            return {"ok": False, "motivo": r.get("motivo")}
        return {"ok": True, "valor": valor, "crescimento": r["crescimento"], "limite": r.get("limite")}

    mercado = resolver(preco)
    if not mercado["ok"]:
        return {"ok": False, "motivo": mercado["motivo"], "taxa": taxa, "origem_taxa": origem_taxa}
    rel_base = resolver(base) if _ok(base) and base > 0 else None
    rel_pond = resolver(ponderado) if _ok(ponderado) and ponderado > 0 else None
    util = lambda x: x if _ok(x) and abs(x) <= HISTORICO_MAX else None  # noqa: E731
    hist = {"fcf": apoio.get("cagr_fcf") if _ok(apoio.get("cagr_fcf")) else None,
            "receita": apoio.get("cagr_receita") if _ok(apoio.get("cagr_receita")) else None}
    hist["fcf_comparavel"] = util(hist["fcf"]) is not None
    hist["receita_comparavel"] = util(hist["receita"]) is not None

    def g_txt(r):
        if r["limite"] == "acima":
            return f"mais de {_pct(r['crescimento'])}"
        if r["limite"] == "abaixo":
            return f"menos de {_pct(r['crescimento'])}"
        return _pct(r["crescimento"])

    leitura = [f"O preço atual equivale a um crescimento do FCF de {g_txt(mercado)} ao ano durante {ANOS} anos "
               f"(depois {_pct(gt)} para sempre), descontado a {_pct(taxa)} ({origem_taxa})."]
    alerta = None
    if rel_base and rel_base["ok"]:
        dif = rel_base["crescimento"] - mercado["crescimento"]
        if dif >= DIFERENCA_RELEVANTE:
            leitura.append(f"O relatório aposta em mais {_pp(dif)} de crescimento por ano do que o mercado: a tese depende "
                           "disso. Confirma no relatório porquê.")
        elif dif <= -DIFERENCA_RELEVANTE:
            leitura.append(f"O relatório é mais prudente do que o mercado ({_pp(dif)} por ano abaixo): o preço já desconta "
                           "mais crescimento do que o cenário base.")
        else:
            leitura.append("O cenário base e o mercado assumem um crescimento parecido.")
    h = util(hist["fcf"]) if util(hist["fcf"]) is not None else util(hist["receita"])
    nome_h = "do FCF" if util(hist["fcf"]) is not None else "da receita"
    if h is not None:
        if mercado["crescimento"] - h >= DIFERENCA_RELEVANTE:
            leitura.append(f"O preço exige crescer acima do histórico {nome_h} ({_pct(h)} ao ano).")
        if rel_base and rel_base["ok"] and rel_base["crescimento"] - h >= DIFERENCA_RELEVANTE:
            alerta = (f"O cenário base do relatório assume um crescimento ({g_txt(rel_base)} ao ano) acima do histórico {nome_h} "
                      f"({_pct(h)}): é a parte mais frágil da tese.")
            leitura.append(alerta)
    return {"ok": True, "motivo": None, "taxa": taxa, "origem_taxa": origem_taxa, "crescimento_terminal": gt, "anos": ANOS,
            "mercado": mercado, "base": rel_base, "ponderado": rel_pond, "historico": hist, "leitura": leitura,
            "alerta": alerta, "limiar": DIFERENCA_RELEVANTE}
