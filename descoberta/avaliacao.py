"""
Valor intrínseco: a empresa está barata face ao que vale?

Três métodos independentes, para não depender de um só número:
1. DCF em três cenários (pessimista, base, otimista), com pressupostos tirados do
   histórico da própria empresa e das previsões dos analistas, nunca inventados.
2. DCF inverso: que crescimento o preço atual já pressupõe. É o teste mais honesto,
   porque não depende de previsões nossas.
3. Múltiplos face à história da própria empresa (EV/EBIT, P/E, rendibilidade do FCF).

Bancos e seguradoras usam o P/VC justificado: (ROE − g) / (r − g).
Todos os valores são calculados na moeda das demonstrações e convertidos para a
moeda de cotação no fim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from metricas import cagr


@dataclass
class Parametros:
    taxa_desconto: float = 0.09          # retorno anual exigido (custo de capital)
    crescimento_terminal: float = 0.025  # crescimento perpétuo depois do período explícito
    anos: int = 10                       # período explícito
    anos_crescimento: int = 3            # anos em que o crescimento inicial se mantém; depois converge para o terminal
    margem_exigida: float = 0.25         # desconto exigido face ao valor esperado para o preço de entrada
    descontar_sbc: bool = True           # tratar a remuneração em ações como custo
    metodo: str = "auto"                 # auto | reportado | normalizado | roic (ver METODOS)
    probabilidades: tuple = (0.25, 0.50, 0.25)  # pessimista, base, otimista


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _linear(x: float, x0: float, p0: float, x1: float, p1: float) -> float:
    """Interpolação linear entre (x0, p0) e (x1, p1), limitada aos extremos."""
    if x0 == x1:
        return p1
    t = min(max((x - x0) / (x1 - x0), 0.0), 1.0)
    return p0 + t * (p1 - p0)


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def _mediana(s: pd.Series) -> float:
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    return float(s.median()) if len(s) else float("nan")


def crescimento_no_ano(t: int, g0: float, gt: float, anos: int, anos_alto: int) -> float:
    """g0 nos primeiros `anos_alto` anos; depois converge linearmente até gt no último ano."""
    if t <= anos_alto:
        return g0
    return g0 + (gt - g0) * (t - anos_alto) / max(anos - anos_alto, 1)


def dcf(receita0: float, g0: float, margem: float, r: float, gt: float, anos: int = 10, anos_alto: int = 3) -> float:
    """Valor da empresa (EV) a partir das receitas, do crescimento e de uma margem de FCF constante."""
    valor, rec, fcf = 0.0, receita0, 0.0
    for t in range(1, anos + 1):
        rec *= 1 + crescimento_no_ano(t, g0, gt, anos, anos_alto)
        fcf = rec * margem
        valor += fcf / (1 + r) ** t
    terminal = fcf * (1 + gt) / (r - gt)
    return valor + terminal / (1 + r) ** anos


def _reinvestimento(g: float, roic: float) -> float:
    """Parte do lucro operacional que tem de ser reinvestida para crescer g com um retorno roic."""
    return min(max(g / roic, 0.0), 1.0) if roic > 0 else 1.0


def dcf_roic(receita0: float, g0: float, margem_nopat: float, roic: float, r: float, gt: float,
             anos: int = 10, anos_alto: int = 3) -> float:
    """EV pelo lucro operacional depois de impostos (NOPAT): FCF = NOPAT × (1 − crescimento / ROIC).
    O capex de crescimento só pesa na medida do que o crescimento exige; o de uma empresa com ROIC alto é pequeno."""
    valor, rec = 0.0, receita0
    for t in range(1, anos + 1):
        g = crescimento_no_ano(t, g0, gt, anos, anos_alto)
        rec *= 1 + g
        valor += rec * margem_nopat * (1 - _reinvestimento(g, roic)) / (1 + r) ** t
    terminal = rec * (1 + gt) * margem_nopat * (1 - _reinvestimento(gt, roic)) / (r - gt)
    return valor + terminal / (1 + r) ** anos


def resolver_crescimento(ev_mercado: float, ev_de) -> tuple[float, str]:
    """Crescimento inicial que iguala ev_de(g) ao valor de mercado (bisseção; ev_de é crescente em g)."""
    lo, hi = -0.30, 0.80
    if ev_de(lo) > ev_mercado:
        return lo, "abaixo do limite"
    if ev_de(hi) < ev_mercado:
        return hi, "acima do limite"
    for _ in range(80):
        mid = (lo + hi) / 2
        if ev_de(mid) < ev_mercado:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, "ok"


def crescimento_implicito(ev_mercado: float, receita0: float, margem: float, r: float, gt: float,
                          anos: int = 10, anos_alto: int = 3) -> tuple[float, str]:
    """Crescimento inicial das receitas que torna o DCF igual ao valor de mercado."""
    if margem <= 0:
        return float("nan"), "margem de FCF não positiva"
    return resolver_crescimento(ev_mercado, lambda g: dcf(receita0, g, margem, r, gt, anos, anos_alto))


def taxa_desconto_empresa(res: dict, p: Parametros) -> tuple[float, list[str]]:
    """Taxa base mais prémios de risco explícitos (nada escondido)."""
    r, ajustes = p.taxa_desconto, []
    tab = res["anual"]
    nd_e = tab["divida_liq_ebitda"].iloc[-1] if "divida_liq_ebitda" in tab else np.nan
    if not res.get("modelo_financeiro") and not pd.isna(nd_e) and nd_e > 3:
        r += 0.01
        ajustes.append("+1 p.p.: dívida líquida acima de 3x o EBITDA")
    beta = res.get("beta")
    if _ok(beta) and beta > 1.4:
        r += 0.005
        ajustes.append(f"+0,5 p.p.: beta de {beta:.2f} (ação muito volátil)")
    mcap = res.get("capitalizacao_fin")
    if res.get("moeda") in ("USD", "EUR", "GBP", "CHF") and _ok(mcap) and mcap < 2e9:
        r += 0.01
        ajustes.append("+1 p.p.: empresa pequena (capitalização abaixo de 2 mil milhões)")
    r = max(r, p.crescimento_terminal + 0.03)
    return r, ajustes


# ---------------------------------------------------------------------------
# Empresas não financeiras: DCF
# ---------------------------------------------------------------------------
def _ancoras_crescimento(res: dict) -> list[tuple[str, float]]:
    tab = res["anual"]
    ancoras = []
    c = cagr(tab["receita"])
    if not math.isnan(c):
        ancoras.append((f"crescimento histórico das receitas ({len(tab['receita'].dropna())} exercícios)", c))
    an = res.get("crescimento_analistas") or {}
    valores = [v["crescimento"] for v in an.values() if _ok(v.get("crescimento"))]
    if valores:
        n = max((v.get("analistas") or 0) for v in an.values())
        ancoras.append((f"previsão dos analistas para os próximos 2 exercícios ({n} analistas)", float(np.mean(valores))))
    return ancoras


METODOS = {
    "reportado": "FCF reportado",
    "normalizado": "FCF normalizado (capex de manutenção)",
    "roic": "Lucro operacional e ROIC",
}
EXPLICACAO_METODOS = {
    "reportado": "Fluxo de caixa livre tal como a empresa o gera: todo o capex conta como custo. É o método mais "
                 "conservador e penaliza as empresas que estão a investir para crescer.",
    "normalizado": "Fluxo de caixa operacional menos o capex de manutenção, estimado pelas amortizações (o que se gasta "
                   "a repor o que se desgasta). O capex de crescimento não reduz o valor, porque se pressupõe que vai "
                   "render. É a lógica dos 'owner earnings' de Buffett.",
    "roic": "Lucro operacional depois de impostos menos o reinvestimento que o crescimento exige (crescimento ÷ ROIC). "
            "Uma empresa com ROIC alto precisa de reinvestir pouco para crescer; uma com ROIC baixo precisa de "
            "reinvestir quase tudo. Penaliza o capex que não rende.",
}
ROIC_MAXIMO = 0.60  # ROIC acima disto não se mantém durante uma década


def _margens(tab: pd.DataFrame, serie: pd.Series, extra: float | None = None) -> pd.Series:
    m = serie.replace([np.inf, -np.inf], np.nan).dropna()
    if extra is not None and _ok(extra):
        m = pd.concat([m, pd.Series([extra])])
    return m


def _tres(m: pd.Series) -> tuple[float, float, float]:
    """(pessimista, base, otimista) a partir do histórico: mínimo, mediana e máximo."""
    base = float(m.median())
    return max(float(m.min()), base * 0.5), base, float(m.max())  # um ano péssimo não é o cenário de 10 anos


def _modelos_disponiveis(res: dict, p: Parametros, receita0: float) -> dict:
    tab, atual = res["anual"], res["atual"]
    modelos = {}

    coluna = "margem_fcf_ajustado" if p.descontar_sbc else "margem_fcf"
    fcf_atual = atual["fcf_ajustado"] if p.descontar_sbc else atual["fcf"]
    m = _margens(tab, tab[coluna], fcf_atual / receita0 if _ok(fcf_atual) and atual["fcf_base"] == "12 meses" else None)
    if len(m) >= 2 and m.median() > 0:
        modelos["reportado"] = {"margens": _tres(m), "hist": m}

    normal = tab["fcf_normalizado"] + (0 if p.descontar_sbc else tab["sbc"].fillna(0))
    m = _margens(tab, normal / tab["receita"])
    if len(m) >= 2 and m.median() > 0:
        modelos["normalizado"] = {"margens": _tres(m), "hist": m}

    imposto = tab["taxa_imposto"].dropna()
    imposto = float(np.clip(imposto.iloc[-1], 0, 0.4)) if len(imposto) else 0.21
    nopat_12m = atual["ebit"] * (1 - imposto) / receita0 if _ok(atual["ebit"]) and atual["ebit_base"] == "12 meses" else None
    m = _margens(tab, tab["margem_nopat"], nopat_12m)
    if (tab["capital_investido_medio"] <= 0).iloc[-1] and (tab["ebit"] > 0).iloc[-1]:
        roic = pd.Series([ROIC_MAXIMO] * 3)  # capital investido negativo: crescer quase não exige capital
    else:
        roic = _margens(tab, tab["roic"]).clip(upper=ROIC_MAXIMO)
    if len(m) >= 2 and m.median() > 0 and len(roic) >= 2 and roic.median() > 0:
        modelos["roic"] = {"margens": _tres(m), "hist": m, "roics": _tres(roic)}
    return modelos


def _diagnostico_capex(tab: pd.DataFrame, r: float) -> dict:
    racio = _mediana(tab["capex"] / tab["amortizacoes"])
    peso = _mediana(tab["capex_crescimento"] / tab["capex"])
    roic = _mediana(tab["roic"])
    c = cagr(tab["receita"])
    return {
        "racio_amortizacoes": racio, "peso_crescimento": peso, "roic_mediano": roic, "crescimento_receitas": c,
        "investimento_crescimento": _ok(racio) and racio >= 1.3 and _ok(c) and c > 0.03,
        "rende": _ok(roic) and roic >= r + 0.03,
    }


def _escolher(p: Parametros, modelos: dict, diag: dict, r: float) -> tuple[list[str], str]:
    def pp(v):
        return f"{v * 100:.1f}%"

    disponiveis = list(modelos)
    if p.metodo != "auto":
        if p.metodo in modelos:
            return [p.metodo], "Método escolhido na barra lateral."
        return [disponiveis[0]], (f"O método escolhido ({METODOS[p.metodo]}) não tem dados suficientes para esta "
                                  f"empresa: usado {METODOS[disponiveis[0]]}.")
    if diag["investimento_crescimento"] and diag["rende"]:
        escolha = [k for k in ("normalizado", "roic") if k in modelos] or disponiveis[:1]
        return escolha, (f"Capex de {diag['racio_amortizacoes']:.1f}x as amortizações, com receitas a crescer "
                         f"{pp(diag['crescimento_receitas'])} ao ano: a empresa está a investir para crescer. Como o ROIC "
                         f"mediano ({pp(diag['roic_mediano'])}) supera o custo de capital ({pp(r)}), esse investimento "
                         "cria valor e não deve contar como custo permanente. Usa a média do FCF normalizado e do "
                         "método ROIC.")
    if diag["investimento_crescimento"]:
        motivo = (f"Capex de {diag['racio_amortizacoes']:.1f}x as amortizações, mas o ROIC mediano "
                  f"({pp(diag['roic_mediano']) if _ok(diag['roic_mediano']) else 'n/d'}) não supera o custo de capital "
                  "em 3 p.p.: não há prova de que o investimento rende. Mantém-se o FCF reportado.")
    elif _ok(diag["racio_amortizacoes"]) and diag["racio_amortizacoes"] >= 1.3:
        motivo = (f"Capex de {diag['racio_amortizacoes']:.1f}x as amortizações, mas as receitas crescem só "
                  f"{pp(diag['crescimento_receitas']) if _ok(diag['crescimento_receitas']) else 'n/d'} ao ano: o capex "
                  "extra não está a gerar crescimento. Em negócios de ativos antigos (ferrovias, utilities, energia), "
                  "repor os ativos custa mais do que a amortização histórica, por isso esse capex é, na prática, "
                  "manutenção. Mantém-se o FCF reportado.")
    else:
        motivo = ("Capex próximo das amortizações: não há investimento de crescimento relevante e o FCF reportado "
                  "representa bem a caixa que o negócio gera.")
    if "reportado" in modelos:
        return ["reportado"], motivo
    # sem FCF positivo e sem prova de que o investimento rende: normalizar o capex seria perdoar capex que destrói valor
    return [], ("o FCF reportado não é positivo de forma sustentada e o ROIC não mostra que o investimento rende "
                "acima do custo de capital; ignorar o capex seria sobrevalorizar a empresa")


def _avaliar_dcf(res: dict, p: Parametros) -> dict:
    tab, atual = res["anual"], res["atual"]
    out = {"metodo": "DCF (fluxo de caixa livre descontado)", "avisos": []}
    receita0 = atual["receita"]
    if not _ok(receita0) or receita0 <= 0:
        out["erro"] = "sem receitas"
        return out

    modelos = _modelos_disponiveis(res, p, receita0)
    if not modelos:
        out["erro"] = ("nem o FCF nem o lucro operacional são positivos de forma sustentada: a empresa ainda não "
                       "gera caixa e um DCF seria pura especulação")
        return out

    ancoras = _ancoras_crescimento(res)
    if not ancoras:
        out["erro"] = "sem base para estimar o crescimento"
        return out
    vals = [g for _, g in ancoras]
    # os analistas veem os próximos 2 anos; o histórico só olha para trás: peso 60/40 quando há ambos
    media = 0.4 * vals[0] + 0.6 * vals[1] if len(vals) == 2 else vals[0]
    g_base = float(np.clip(media, -0.05, 0.20))
    g_pess = float(np.clip(min(min(vals) - 0.03, g_base * 0.5 if g_base > 0 else g_base - 0.03), -0.10, 0.15))
    g_otim = float(np.clip(max(max(vals), g_base * 1.3, g_base + 0.02), -0.05, 0.30))
    if media > 0.20:
        out["avisos"].append(f"O crescimento de partida ({media*100:.1f}%) foi limitado a 20% no cenário base. "
                             "Crescimentos acima disso raramente se mantêm durante anos.")
    out["divergencia"] = len(vals) == 2 and abs(vals[0] - vals[1]) > 0.10
    if out["divergencia"]:
        out["avisos"].append(f"O histórico ({vals[0]*100:.1f}%) e a previsão dos analistas ({vals[1]*100:.1f}%) divergem "
                             "muito: o negócio está a mudar. O cenário base é pouco fiável; decide com os relatórios.")

    r, ajustes = taxa_desconto_empresa(res, p)
    gt = p.crescimento_terminal
    divida = atual["divida_liquida"] if _ok(atual["divida_liquida"]) else 0.0
    minorit = atual.get("minoritarios") or 0.0
    acoes = res["acoes_equivalentes"]

    def por_unidade(ev):
        return (ev - divida - minorit) / acoes

    diag = _diagnostico_capex(tab, r)
    escolha, motivo = _escolher(p, modelos, diag, r)
    if not escolha:
        out.update({"erro": motivo, "capex": diag})
        return out

    def ev_modelo(chave: str, g: float, i: int, rr: float, gg: float) -> float:
        """EV de um método, no cenário i (0 pessimista, 1 base, 2 otimista)."""
        mg = modelos[chave]["margens"][i]
        if chave == "roic":
            return dcf_roic(receita0, g, mg, modelos[chave]["roics"][i], rr, gg, p.anos, p.anos_crescimento)
        return dcf(receita0, g, mg, rr, gg, p.anos, p.anos_crescimento)

    def ev_escolha(g: float, i: int, rr: float, gg: float) -> float:
        return float(np.mean([ev_modelo(k, g, i, rr, gg) for k in escolha]))

    def margem_efetiva(chave: str, g: float, i: int) -> float:
        """Margem de FCF do primeiro ano (no método ROIC, depois de descontar o reinvestimento)."""
        mg = modelos[chave]["margens"][i]
        return mg * (1 - _reinvestimento(g, modelos[chave]["roics"][i])) if chave == "roic" else mg

    gs = (g_pess, g_base, g_otim)
    nomes = ("Pessimista", "Base", "Otimista")
    cenarios = [{"cenario": nomes[i], "crescimento_ano1": gs[i], "probabilidade": p.probabilidades[i],
                 "margem_fcf": float(np.mean([margem_efetiva(k, gs[i], i) for k in escolha])),
                 "valor_fin": por_unidade(ev_escolha(gs[i], i, r, gt))} for i in range(3)]
    esperado = sum(c["valor_fin"] * c["probabilidade"] for c in cenarios)

    # os três métodos lado a lado, para comparação (a escolha só decide qual conta para o veredito)
    metodos = []
    for chave, mod in modelos.items():
        valores = [por_unidade(ev_modelo(chave, gs[i], i, r, gt)) for i in range(3)]
        metodos.append({"chave": chave, "nome": METODOS[chave], "explicacao": EXPLICACAO_METODOS[chave],
                        "usado": chave in escolha, "valores_fin": valores,
                        "esperado_fin": sum(v * pb for v, pb in zip(valores, p.probabilidades)),
                        "margens": mod["margens"], "roics": mod.get("roics"),
                        "hist": [float(x) for x in mod["hist"]]})

    # sensibilidade do cenário base à taxa de desconto e ao crescimento terminal
    sens = {}
    for dr in (-0.01, 0.0, 0.01):
        for dg in (-0.005, 0.0, 0.005):
            rr, gg = r + dr, gt + dg
            sens[(rr, gg)] = por_unidade(ev_escolha(g_base, 1, rr, gg)) if rr > gg + 0.01 else np.nan

    # DCF inverso, com o mesmo método
    implicito, estado = (float("nan"), "sem capitalização")
    if _ok(res.get("ev_atual")):
        implicito, estado = resolver_crescimento(res["ev_atual"], lambda g: ev_escolha(g, 1, r, gt))

    out.update({
        "metodo": " + ".join(METODOS[k] for k in escolha), "escolha": escolha, "motivo_metodo": motivo,
        "metodos": metodos, "capex": diag,
        "taxa_desconto": r, "ajustes_taxa": ajustes, "crescimento_terminal": gt,
        "receita_base": receita0, "receita_base_origem": atual["receita_base"],
        "ancoras": ancoras, "margens_historicas": metodos[[m["chave"] for m in metodos].index(escolha[0])]["hist"],
        "cenarios": cenarios, "valor_esperado_fin": esperado, "sensibilidade": sens,
        "crescimento_implicito": implicito, "crescimento_implicito_estado": estado,
        "crescimento_base": g_base,
    })
    return out


# ---------------------------------------------------------------------------
# Bancos e seguradoras: P/VC justificado
# ---------------------------------------------------------------------------
def _avaliar_financeira(res: dict, p: Parametros) -> dict:
    tab, atual = res["anual"], res["atual"]
    out = {"metodo": "P/VC justificado (ROE − g) / (r − g)", "avisos": []}
    roe = tab["roe"].replace([np.inf, -np.inf], np.nan).dropna()
    cp = atual.get("capital_proprio")
    acoes = res["acoes_equivalentes"]
    if len(roe) < 2 or not _ok(cp) or cp <= 0 or not _ok(acoes):
        out["erro"] = "sem histórico de ROE ou capital próprio"
        return out
    r, ajustes = taxa_desconto_empresa(res, p)
    distrib = ((tab["dividendos"].fillna(0) + tab["recompras"].fillna(0)) / tab["lucro_liquido"]).where(tab["lucro_liquido"] > 0)
    payout = _mediana(distrib.clip(0, 1.5))
    roe_base = float(roe.median())
    g = float(np.clip(roe_base * (1 - payout) if _ok(payout) else p.crescimento_terminal, 0.0, min(0.06, r - 0.02)))
    vc_unidade = cp / acoes

    cenarios = []
    for nome, roe_c, prob in [("Pessimista", float(roe.min()), p.probabilidades[0]),
                              ("Base", roe_base, p.probabilidades[1]),
                              ("Otimista", float(min(roe.max(), 0.30)), p.probabilidades[2])]:
        pvc = max((roe_c - g) / (r - g), 0.0)
        cenarios.append({"cenario": nome, "roe": roe_c, "pvc_justificado": pvc, "probabilidade": prob,
                         "valor_fin": vc_unidade * pvc})
    esperado = sum(c["valor_fin"] * c["probabilidade"] for c in cenarios)
    pvc_mercado = res["capitalizacao_fin"] / cp if _ok(res.get("capitalizacao_fin")) else float("nan")
    roe_implicito = pvc_mercado * (r - g) + g if _ok(pvc_mercado) else float("nan")
    out.update({
        "taxa_desconto": r, "ajustes_taxa": ajustes, "crescimento_sustentavel": g, "payout": payout,
        "valor_contabilistico_unidade_fin": vc_unidade, "cenarios": cenarios, "valor_esperado_fin": esperado,
        "pvc_mercado": pvc_mercado, "roe_implicito": roe_implicito, "roe_base": roe_base,
    })
    return out


# ---------------------------------------------------------------------------
# Pontuação do preço (0 = caro, 100 = muito barato)
# ---------------------------------------------------------------------------
def _pontuacao_preco(res: dict, val: dict) -> tuple[float | None, float, list[dict]]:
    tab = res["anual"]
    comp = []

    def add(nome, pontos, maximo, detalhe):
        comp.append({"componente": nome, "pontos": pontos, "maximo": maximo, "detalhe": detalhe})

    desconto = val.get("desconto")
    financeira = res.get("modelo_financeiro")
    max_desc = 50 if financeira else 40
    if _ok(desconto):
        add("Desconto face ao valor esperado", _linear(desconto, -0.30, 0, 0.30, max_desc), max_desc,
            f"{desconto*100:+.0f}% (positivo = preço abaixo do valor)")
    else:
        add("Desconto face ao valor esperado", None, max_desc, val.get("erro") or "sem valor intrínseco")

    if financeira:
        ri, rb = val.get("roe_implicito"), val.get("roe_base")
        if _ok(ri) and _ok(rb):
            add("ROE que o preço pressupõe vs ROE histórico", _linear(ri - rb, 0.05, 0, -0.03, 20), 20,
                f"o preço pressupõe ROE de {ri*100:.1f}% vs mediana histórica de {rb*100:.1f}%")
        else:
            add("ROE que o preço pressupõe vs ROE histórico", None, 20, "sem dados")
    else:
        gi, gb = val.get("crescimento_implicito"), val.get("crescimento_base")
        if _ok(gi) and _ok(gb) and val.get("crescimento_implicito_estado") in ("ok", "abaixo do limite", "acima do limite"):
            add("Crescimento que o preço pressupõe (DCF inverso)", _linear(gi - gb, 0.05, 0, -0.03, 20), 20,
                f"o preço pressupõe {gi*100:.1f}% ao ano vs {gb*100:.1f}% no cenário base")
        else:
            add("Crescimento que o preço pressupõe (DCF inverso)", None, 20, "sem dados")

        mcap, fcf = res.get("capitalizacao_fin"), res["atual"].get("fcf_ajustado")
        nome = "Rendibilidade do FCF (FCF ajustado / capitalização)"
        if "normalizado" in val.get("escolha", []):  # coerente com o método: o capex de crescimento não conta
            normal = tab["fcf_normalizado"].dropna()
            if len(normal):
                fcf, nome = float(normal.iloc[-1]), "Rendibilidade do FCF normalizado (sem capex de crescimento)"
        if _ok(mcap) and _ok(fcf) and mcap > 0:
            rend = fcf / mcap
            add(nome, _linear(rend, 0.02, 0, 0.07, 15), 15, f"{rend*100:.1f}% ao ano")
        else:
            add(nome, None, 15, "sem dados")

        ev_ebit, med = res.get("ev_ebit_atual"), _mediana(tab["ev_ebit_fim"])
        if _ok(ev_ebit) and _ok(med) and len(tab["ev_ebit_fim"].dropna()) >= 3:
            rel = ev_ebit / med - 1
            add("EV/EBIT face à história da empresa", _linear(rel, 0.30, 0, -0.20, 15), 15,
                f"{ev_ebit:.1f}x vs mediana de {med:.1f}x ({rel*100:+.0f}%)")
        else:
            add("EV/EBIT face à história da empresa", None, 15, "histórico insuficiente")

    pe, pe_med = res.get("pe_atual"), res.get("pe_mediana")
    max_pe = 15 if financeira else 10
    if _ok(pe) and _ok(pe_med):
        rel = pe / pe_med - 1
        add("P/E face à história da empresa", _linear(rel, 0.30, 0, -0.20, max_pe), max_pe,
            f"{pe:.1f}x vs mediana de {pe_med:.1f}x ({rel*100:+.0f}%)")
    else:
        add("P/E face à história da empresa", None, max_pe, "histórico insuficiente")

    if financeira:
        pvc_hist = (tab["capitalizacao_fim"] / tab["capital_proprio"]).dropna()
        pvc = val.get("pvc_mercado")
        if _ok(pvc) and len(pvc_hist) >= 3:
            rel = pvc / float(pvc_hist.median()) - 1
            add("P/VC face à história da empresa", _linear(rel, 0.30, 0, -0.20, 15), 15,
                f"{pvc:.2f}x vs mediana de {pvc_hist.median():.2f}x ({rel*100:+.0f}%)")
        else:
            add("P/VC face à história da empresa", None, 15, "histórico insuficiente")

    avaliados = [c for c in comp if c["pontos"] is not None]
    maximo_total = sum(c["maximo"] for c in comp)
    maximo_avaliado = sum(c["maximo"] for c in avaliados)
    pont = 100 * sum(c["pontos"] for c in avaliados) / maximo_avaliado if maximo_avaliado else None
    return pont, (maximo_avaliado / maximo_total if maximo_total else 0.0), comp


def _valor_por_multiplos(res: dict) -> list[dict]:
    """Valor por ação se a empresa voltasse a negociar aos múltiplos da sua própria história
    (mínimo, mediana e máximo de fim de exercício), aplicados aos resultados dos últimos 12 meses."""
    tab, atual = res["anual"], res["atual"]
    acoes, fx = res.get("acoes_equivalentes"), res.get("fx_atual") or 1.0
    if not _ok(acoes) or not acoes:
        return []
    out = []
    pes = tab["pe_fim_exercicio"].dropna()
    ll = atual.get("lucro_liquido")
    if len(pes) >= 3 and _ok(ll) and ll > 0:
        eps = ll / acoes
        out.append({"metodo": "P/E histórico", "min": pes.min() * eps * fx, "mediana": pes.median() * eps * fx,
                    "max": pes.max() * eps * fx})
    evs = tab["ev_ebit_fim"].dropna()
    ebit, nd = atual.get("ebit"), atual.get("divida_liquida")
    if len(evs) >= 3 and _ok(ebit) and ebit > 0 and _ok(nd) and not res.get("modelo_financeiro"):
        minorit = atual.get("minoritarios") or 0.0

        def valor(m):
            return (m * ebit - nd - minorit) / acoes * fx

        out.append({"metodo": "EV/EBIT histórico", "min": valor(evs.min()), "mediana": valor(evs.median()),
                    "max": valor(evs.max())})
    return out


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def avaliar(res: dict, p: Parametros | None = None) -> dict:
    p = p or Parametros()
    if res.get("preco_fin") is None or res.get("acoes_equivalentes") is None:
        val = {"erro": "sem preço comparável com as demonstrações (ver avisos)", "avisos": []}
    elif res.get("modelo_financeiro"):
        val = _avaliar_financeira(res, p)
    else:
        val = _avaliar_dcf(res, p)

    fx = res.get("fx_atual") or 1.0
    if "erro" not in val and val["valor_esperado_fin"] <= 0:
        val["erro"] = ("o valor das operações não chega para cobrir a dívida líquida: neste modelo o capital próprio "
                       "não tem valor intrínseco positivo")
    if "erro" not in val:
        esperado = val["valor_esperado_fin"]
        val["valor_esperado"] = esperado * fx  # na moeda de cotação (principal)
        for c in val["cenarios"]:
            c["valor"] = c["valor_fin"] * fx
        val["desconto"] = 1 - res["preco_fin"] / esperado if esperado > 0 else float("-inf")
        val["preco_entrada"] = esperado * (1 - p.margem_exigida) * fx if esperado > 0 else None
        pess = val["cenarios"][0]["valor_fin"]
        val["queda_ate_pessimista"] = pess / res["preco_fin"] - 1
        if "sensibilidade" in val:
            val["sensibilidade"] = {k: v * fx for k, v in val["sensibilidade"].items()}
        for m in val.get("metodos", []):
            m["valores"] = [v * fx for v in m["valores_fin"]]
            m["esperado"] = m["esperado_fin"] * fx
            m["entrada"] = m["esperado_fin"] * (1 - p.margem_exigida) * fx
    val["multiplos"] = _valor_por_multiplos(res) if res.get("preco_fin") is not None else []
    pont, conf, comp = _pontuacao_preco(res, val)
    val.update({"pontuacao": pont, "confianca": conf, "componentes": comp, "parametros": p})
    return val
