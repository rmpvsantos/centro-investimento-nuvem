"""
Candidatas e concentração da carteira (pedido do Rui a 18/10/2026).

1. Resumo de cada empresa (resumo_de): o que a Decisão calculou da última vez (sinal, cenários do relatório, preço,
   filtros, contraprova), guardado em decisoes_resumo.json sempre que a página Decisão de uma empresa é calculada.
   Assim a lista compara todas as empresas sem voltar a ir ao Yahoo.
2. Ordenação: primeiro as que têm os cenários do relatório verificados e nenhum filtro falhado, pelo retorno até ao
   valor ponderado (ponderado ÷ preço − 1); depois as que falham um filtro; por fim as sem cenários válidos, sem lugar
   no ranking (sem cenários não há retorno esperado: não se adivinha).
3. Concentração: pesos das tuas posições e setores, da cópia da Carteira (carteira_dados.json: quantidades dos
   movimentos × preço em euros das cotações; setores da classificação que preenches na Carteira). Cada peso conta só
   dentro do seu grupo (pedido do Rui a 18/10/2026): uma ação sobre o total investido em ações, um ETF sobre o total
   investido em ETF; os setores e os avisos são só das ações. Limites em regras_cowork.LIMIARES (peso_max_posicao,
   peso_max_setor).
Funções puras (sem rede): testadas em testes/teste_candidatas.py.
"""
from __future__ import annotations

import math
from collections import defaultdict

SETOR_YAHOO = {"Technology": "Tecnologia", "Healthcare": "Saúde", "Financial Services": "Financeiro",
               "Industrials": "Indústria", "Consumer Defensive": "Consumo básico", "Consumer Cyclical": "Consumo discricionário",
               "Communication Services": "Comunicação", "Energy": "Energia", "Basic Materials": "Materiais",
               "Real Estate": "Imobiliário", "Utilities": "Utilities"}
SEM_SETOR = {"ETF diversificado", "Ouro", "Poupança e outros"}


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v) and not math.isinf(v)


# ---------------------------------------------------------------------------
# 1. Resumo de uma empresa (a partir do resultado de bloco_decisao)
# ---------------------------------------------------------------------------
def resumo_de(r: dict, calculado: str) -> dict:
    d, v = r.get("decisao") or {}, r.get("verificacao") or {}
    cen, cr = v.get("cenarios") or {}, r.get("cenarios_relatorio") or {}
    filtros = v.get("filtros") or {}
    cp = r.get("contraprova") or {}
    g = lambda x: (x or {}).get("crescimento") if (x or {}).get("ok") else None  # noqa: E731
    return {
        "ticker": r.get("ticker"), "nome": r.get("nome"), "lista": r.get("lista"), "calculado": calculado,
        "nivel": d.get("nivel"), "rotulo": d.get("rotulo"), "mostrar": (d.get("cowork") or {}).get("mostrar") or d.get("rotulo"),
        "origem_nivel": d.get("origem_nivel"), "preco": r.get("preco"), "moeda": (r.get("preco_info") or {}).get("moeda") or r.get("moeda"),
        "preco_quando": (r.get("preco_info") or {}).get("quando"),
        "verificado": v.get("estado") == "verificado", "motivo": v.get("motivo"),
        "ponderado": cen.get("ponderado"), "desconto": cen.get("desconto"), "assimetria": cen.get("assimetria"),
        "entrada": (cen.get("entradas") or {}).get("compra"),
        "bear": (cr.get("bear") or {}).get("valor"), "base": (cr.get("base") or {}).get("valor"), "bull": (cr.get("bull") or {}).get("valor"),
        "data_cenarios": v.get("data_relatorio"), "recomendacao": v.get("recomendacao_ficha"), "divergente": v.get("divergente"),
        "filtros_passam": filtros.get("passam"), "falhados": filtros.get("falhados") or [],
        "por_verificar": (filtros.get("por_verificar") or []) + (filtros.get("parciais") or []),
        "contraprova": {"mercado": g(cp.get("mercado")), "base": g(cp.get("base")), "alerta": cp.get("alerta"),
                        "motivo": None if cp.get("ok") else cp.get("motivo")},
        "setor_yahoo": r.get("setor"), "industria": r.get("industria"), "tipo_setor": v.get("tipo_setor"),
    }


# ---------------------------------------------------------------------------
# 3. Pesos da carteira (cópia da Carteira de Ativos)
# ---------------------------------------------------------------------------
def pesos_carteira(exportacao: dict | None, yahoo_da_folha) -> dict:
    if not exportacao or not (exportacao.get("dados") or {}).get("movimentos"):
        return {"ok": False, "motivo": "Ainda não há cópia da Carteira (abre a Carteira uma vez)."}
    d = exportacao["dados"]
    qtd = defaultdict(float)
    for m in d["movimentos"]:
        if m.get("tipo") in ("Compra", "Venda") and _ok(m.get("qtd")) and m.get("ticker"):
            qtd[m["ticker"]] += m["qtd"] if m["tipo"] == "Compra" else -m["qtd"]
    cot = {c.get("ticker"): c for c in d.get("cotacoes") or []}
    classif = d.get("classificacao") or {}
    posicoes, sem_preco = [], []
    for t, q in qtd.items():
        if q <= 1e-6:
            continue
        c = cot.get(t) or {}
        if c.get("tipo") not in ("Ação", "ETF"):
            continue
        if not _ok(c.get("precoEur")):
            sem_preco.append(t)
            continue
        posicoes.append({"folha": t, "yahoo": yahoo_da_folha(t), "nome": c.get("nome"), "tipo": c["tipo"],
                         "setor": (classif.get(t) or {}).get("setor") or "Por classificar", "valor": q * c["precoEur"]})
    # Pedido do Rui (18/10/2026): cada peso conta só dentro do seu grupo — uma ação sobre o total investido em ações, um
    # ETF sobre o total investido em ETF; os setores contam-se só nas ações
    totais = {t: sum(p["valor"] for p in posicoes if p["tipo"] == t) for t in ("Ação", "ETF")}
    if sum(totais.values()) <= 0:
        return {"ok": False, "motivo": "A cópia da Carteira não tem posições com preço."}
    setores = defaultdict(float)
    for p in posicoes:
        p["peso"] = p["valor"] / totais[p["tipo"]]
        if p["tipo"] == "Ação":
            setores[p["setor"]] += p["peso"]
    return {"ok": True, "exportado": exportacao.get("exportado"), "total_eur": totais["Ação"], "total_etf_eur": totais["ETF"],
            "posicoes": sorted(posicoes, key=lambda p: -p["valor"]),
            "setores": sorted(({"setor": s, "peso": w, "conta": s not in SEM_SETOR} for s, w in setores.items()), key=lambda x: -x["peso"]),
            "etfs": sorted(({"folha": p["folha"], "nome": p["nome"], "setor": p["setor"], "peso": p["peso"]} for p in posicoes
                            if p["tipo"] == "ETF"), key=lambda x: -x["peso"]),
            "sem_preco": sem_preco}


def posicao_de(pesos: dict, ticker: str, outra_cotacao: dict) -> dict | None:
    """A posição da carteira que corresponde a um ticker do Yahoo da Análise (a mesma cotação ou a da outra bolsa)."""
    if not pesos.get("ok"):
        return None
    t = ticker.upper()
    alt = outra_cotacao.get(t)
    return next((p for p in pesos["posicoes"] if p["yahoo"].upper() == t or (alt and p["folha"] == alt)), None)


# ---------------------------------------------------------------------------
# 2. Ordenação e avisos
# ---------------------------------------------------------------------------
def montar(resumos: dict, listas: dict, pesos: dict, limiares: dict, outra_cotacao: dict) -> dict:
    """resumos: {ticker: resumo_de(...)}; listas: {"carteira": [{"ticker", "nome"}], "observacao": [...]}"""
    lim_pos, lim_set = limiares["peso_max_posicao"], limiares["peso_max_setor"]
    peso_setor = {s["setor"]: s["peso"] for s in pesos.get("setores") or []}
    linhas = []
    for lista in ("carteira", "observacao"):
        for x in listas.get(lista) or []:
            t = x["ticker"].upper()
            r = resumos.get(t) or {}
            pos = posicao_de(pesos, t, outra_cotacao)
            setor = (pos or {}).get("setor") or SETOR_YAHOO.get(r.get("setor_yahoo") or "", r.get("setor_yahoo"))
            preco = r.get("preco")
            ok = r.get("verificado") and _ok(preco) and preco > 0 and _ok(r.get("ponderado"))
            rel = lambda v: v / preco - 1 if ok and _ok(v) else None  # noqa: E731
            linha = {"ticker": t, "nome": r.get("nome") or x.get("nome"), "lista": lista, "calculado": r.get("calculado"),
                     "nivel": r.get("nivel"), "mostrar": r.get("mostrar"), "origem_nivel": r.get("origem_nivel"),
                     "preco": preco, "moeda": r.get("moeda"), "verificado": bool(ok),
                     "motivo": None if ok else (r.get("motivo") or ("ainda não calculada" if not r else "sem preço")),
                     "retorno_esperado": rel(r.get("ponderado")), "ganho_base": rel(r.get("base")), "perda_bear": rel(r.get("bear")),
                     "ponderado": r.get("ponderado"), "assimetria": r.get("assimetria"), "desconto": r.get("desconto"),
                     "entrada": r.get("entrada"), "falta_descer": (1 - r["entrada"] / preco) if ok and _ok(r.get("entrada")) and preco > r["entrada"] else None,
                     "falhados": r.get("falhados") or [], "por_verificar": r.get("por_verificar") or [],
                     "recomendacao": r.get("recomendacao"), "data_cenarios": r.get("data_cenarios"),
                     "contraprova": r.get("contraprova") or {}, "setor": setor, "peso": (pos or {}).get("peso"),
                     "peso_setor": peso_setor.get(setor) if setor else None, "avisos": []}
            if linha["peso"] is not None and linha["peso"] > lim_pos:
                linha["avisos"].append(f"posição com {_pct(linha['peso'])} das ações (aviso a partir de {_pct(lim_pos, 0)})")
            if linha["nivel"] in ("comprar", "acumular") and setor and setor not in SEM_SETOR and (linha["peso_setor"] or 0) > lim_set:
                linha["avisos"].append(f"comprar aumenta {setor}, que já pesa {_pct(linha['peso_setor'])} das ações (aviso a partir de {_pct(lim_set, 0)})")
            linhas.append(linha)

    def chave(l):
        grupo = 0 if l["verificado"] and not l["falhados"] else 1 if l["verificado"] else 2
        return (grupo, -(l["retorno_esperado"] or 0) if grupo < 2 else 0, l["ticker"])

    linhas.sort(key=chave)
    pos = 0
    for l in linhas:
        if l["verificado"]:
            pos += 1
            l["lugar"] = pos
    avisos = []
    if pesos.get("ok"):
        avisos += [f"{p['folha']} pesa {_pct(p['peso'])} das ações (aviso a partir de {_pct(lim_pos, 0)})"
                   for p in pesos["posicoes"] if p["tipo"] == "Ação" and p["peso"] > lim_pos]
        avisos += [f"O setor {s['setor']} pesa {_pct(s['peso'])} das ações (aviso a partir de {_pct(lim_set, 0)})"
                   for s in pesos["setores"] if s["conta"] and s["peso"] > lim_set]
    return {"linhas": linhas, "carteira": pesos, "avisos": avisos,
            "limiares": {"peso_max_posicao": lim_pos, "peso_max_setor": lim_set}}


def _pct(x: float, casas: int = 1) -> str:
    return f"{x * 100:.{casas}f}%".replace(".", ",")
