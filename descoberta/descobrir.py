"""
Descoberta em lote pela linha de comandos (para universos grandes, por exemplo durante a noite).

Exemplos:
    python descobrir.py --regioes us --mcap 5000 --roic 12 --ev-ebit 25
    python descobrir.py --regioes europa --mcap 2000 --setores Technology,Healthcare --divida 2
    python descobrir.py --regioes us,europa --mcap 10000 --limite 300

Os pré-filtros disponíveis são os de filtros.FILTROS_YAHOO (python descobrir.py --help).
Os resultados ficam em resultados/descoberta_<data>.xlsx e aparecem no separador
"Descobrir oportunidades" da aplicação.
"""

from __future__ import annotations

import argparse
import sys

from avaliacao import Parametros
from filtros import FILTROS_YAHOO, SETORES
from universo import (ORDENS, REGIOES, REGIOES_EUROPA, analisar_lote, gravar_resultados, listar_universo, ordenar,
                      paises_das_regioes)


def main() -> int:
    ap = argparse.ArgumentParser(description="Descobrir empresas de qualidade com desconto (EUA, Canadá e Europa).")
    ap.add_argument("--regioes", default="us,ca,europa",
                    help="us, ca, europa, ou códigos separados por vírgula (gb,de,fr,pt...)")
    ap.add_argument("--mcap", type=float, default=2000, help="capitalização mínima, em milhões de USD")
    ap.add_argument("--setores", default="", help="setores do Yahoo separados por vírgula: " + ", ".join(SETORES))
    for _, chave, rotulo, *_resto in FILTROS_YAHOO:
        ap.add_argument(f"--{chave.replace('_', '-')}", dest=chave, type=float, default=None,
                        help=rotulo.replace("%", "%%"))
    ap.add_argument("--limite", type=int, default=None, help="analisar só N empresas (pela ordem de --ordem)")
    ap.add_argument("--ordem", type=int, default=0, choices=range(len(ORDENS)),
                    help="; ".join(f"{i} = {o}" for i, o in enumerate(ORDENS)))
    ap.add_argument("--taxa", type=float, default=9.0, help="taxa de desconto base em %%")
    args = ap.parse_args()

    regioes = []
    for r in args.regioes.lower().split(","):
        r = r.strip()
        regioes += REGIOES_EUROPA if r == "europa" else [r]
    invalidas = [r for r in regioes if r not in REGIOES]
    if invalidas:
        print(f"Regiões desconhecidas: {invalidas}. Válidas: {', '.join(REGIOES)} ou 'europa'.")
        return 1
    setores = [s.strip() for s in args.setores.split(",") if s.strip()]
    if any(s not in SETORES for s in setores):
        print(f"Setores desconhecidos. Válidos: {', '.join(SETORES)}")
        return 1

    condicoes, descricao = [], [f"Regiões: {args.regioes}", f"capitalização ≥ {args.mcap:,.0f} M USD"]
    if setores:
        descricao.append("setores: " + ", ".join(SETORES[s] for s in setores))
    for _, chave, rotulo, campo, operador, *_resto in FILTROS_YAHOO:
        valor = getattr(args, chave)
        if valor is not None:
            condicoes.append((campo, operador, valor))
            descricao.append(f"{rotulo}: {valor:g}")

    def prog(frac, texto):
        print(f"\r[{frac*100:5.1f}%] {texto:<60}", end="", flush=True)

    uni = ordenar(listar_universo(regioes, args.mcap * 1e6, condicoes, setores, progresso=prog), ORDENS[args.ordem])
    print(f"\n{len(uni)} empresas no universo.")
    if uni.empty:
        return 0
    tickers = uni["Ticker"].tolist()[: args.limite] if args.limite else uni["Ticker"].tolist()
    df, falhas = analisar_lote(tickers, Parametros(taxa_desconto=args.taxa / 100), progresso=prog,
                               paises=paises_das_regioes(regioes))
    print()
    descricao.append(f"{len(df)} analisadas, {len(falhas)} sem análise")
    if not df.empty:
        caminho = gravar_resultados(df, "; ".join(descricao))
        print(f"Resultados: {caminho}")
        print(df["Classificação"].value_counts().to_string())
        cand = df[df["ordem"] == 0]
        if not cand.empty:
            print("\nBom negócio a preço atrativo:")
            print(cand[["Ticker", "Empresa", "Qualidade", "Preço (nota)", "Desconto"]].to_string(index=False))
    print(f"\n{len(falhas)} sem análise (dados em falta ou sede fora do âmbito).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
