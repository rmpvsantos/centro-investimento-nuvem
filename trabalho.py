"""
Trabalhador da nuvem do Centro de Investimento (corre no GitHub Actions; gratuito).

Uma volta:
  1. busca ao Google Drive do Rui (pela ligação Apps Script) os dados de trabalho (listas, notas, regras, pesquisas,
     resultados), a cópia dos relatórios do Cowork e os ficheiros de sincronização da Carteira;
  2. arranca os programas tal como estão no PC: Decisão de Investimento (Análise Fundamental, porta 8600) e Centro de
     Investimento (Pesquisa, porta 8700), com a Descoberta de Ações por baixo;
  3. faz os pedidos que a app deixou (gravar notas, mover para a carteira, analisar uma empresa nova, procurar agora…);
  4. calcula o que as páginas mostram (as «respostas») e devolve tudo ao Drive.

A app nunca fala com este programa: lê as respostas no Drive (pela ligação) e deixa lá os pedidos. Os relatórios só se
leem; mudar uma empresa de lista regista a mudança de pasta, que o PC faz quando estiver ligado (mudancas_pastas.json).

Uso: python trabalho.py [--tudo] [--pesquisa]
  --tudo      recalcula todas as empresas seguidas (execuções agendadas)
  --pesquisa  corre as pesquisas automáticas guardadas (segundas-feiras)
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
TRAB = Path(os.environ.get("TRAB_DIR") or "/tmp/centro")
CACHE = Path(os.environ.get("TRAB_CACHE") or "/tmp/centro-cache")
LIGACAO = os.environ.get("LIGACAO_URL", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
EXECUCAO = os.environ.get("GITHUB_RUN_ID", "")

PASTAS = {
    "relatorios": TRAB / "relatorios",
    "decisao_dados": TRAB / "decisao_dados",
    "centro_dados": TRAB / "centro_dados",
    "pedidos_cowork": TRAB / "pedidos_cowork",
    "sync": TRAB / "sync",
}
DESCOBERTA = RAIZ / "descoberta"
# Pacotes de dados no Drive (pasta «Centro - nuvem»): nome -> [(pasta local, prefixo no pacote)]
PACOTES = {
    "dados_decisao.json": [(PASTAS["decisao_dados"], "")],
    "dados_centro.json": [(PASTAS["centro_dados"], ""), (PASTAS["pedidos_cowork"], "pedidos_cowork/")],
    "dados_descoberta.json": [(DESCOBERTA / "pesquisas", "pesquisas/"), (DESCOBERTA / "resultados", "resultados/")],
}
FICHEIROS_SYNC = ["carteira_dados.json", "sincronizacao_carteira.json", "tecnica_carteira.json", "triagens_carteira.json",
                  "pedidos_carteira.json"]
PORTAS = {"d": int(os.environ.get("TRAB_PORTA_D") or 8600), "c": int(os.environ.get("TRAB_PORTA_C") or 8700)}
MUDANCAS = PASTAS["centro_dados"] / "mudancas_pastas.json"
VISTOS = PASTAS["centro_dados"] / "vistos_nuvem.json"  # empresas abertas na app (recalculam-se nas voltas agendadas)

# O que as páginas mostram sem ser de uma empresa
GLOBAIS_D = ["/api/indice", "/api/listas", "/api/por_decidir", "/api/regras", "/api/calendario", "/api/relatorios-em-falta",
             "/api/transferencias", "/api/noticias/recentes?dias=2", "/api/pesquisa?q=&ocultos=false"]
GLOBAIS_C = ["/api/decisao/resumo", "/api/descoberta", "/api/pesquisas", "/api/pesquisas/resultados", "/api/pesquisa/opcoes",
             "/api/cowork/pedidos", "/api/decisao/por_decidir", "/api/estado"]
# Por empresa (a ordem conta: a decisão aquece as caches de tudo o resto)
EMPRESA_D = ["decisao", "triagem", "fundamental", "tecnica", "qualitativa", "noticias"]

REGISTO: list[str] = []


def registar(msg: str) -> None:
    linha = f"{datetime.now(timezone.utc):%H:%M:%S} {msg}"
    REGISTO.append(linha)
    print(linha, flush=True)


# ---------------------------------------------------------------------------------------------------------------------
# Ligação (Apps Script): o trabalhador prova quem é com o token desta execução do GitHub Actions
# ---------------------------------------------------------------------------------------------------------------------
def ligacao(fn: str, *args, tentativas: int = 3):
    corpo = json.dumps({"fn": fn, "args": list(args), "github": TOKEN, "execucao": EXECUCAO}).encode("utf-8")
    for i in range(tentativas):
        try:
            pedido = urllib.request.Request(LIGACAO, data=corpo, headers={"Content-Type": "text/plain;charset=utf-8"}, method="POST")
            with urllib.request.urlopen(pedido, timeout=300) as r:
                j = json.loads(r.read().decode("utf-8"))
            if j.get("erro"):
                raise RuntimeError(f"{fn}: {j['erro']}")
            return j.get("valor")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if i == tentativas - 1:
                raise RuntimeError(f"{fn}: a ligação não respondeu ({e})") from e
            time.sleep(5 * (i + 1))


# ---------------------------------------------------------------------------------------------------------------------
# Pacotes de dados: ficheiros de trabalho dos programas, comprimidos, num ficheiro do Drive por programa
# ---------------------------------------------------------------------------------------------------------------------
def empacotar(nome: str) -> str:
    ficheiros, datas = {}, {}
    for pasta, prefixo in PACOTES[nome]:
        if not pasta.exists():
            continue
        for f in sorted(pasta.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith((".tmp", ".lock")):
                rel = prefixo + f.relative_to(pasta).as_posix()
                ficheiros[rel] = base64.b64encode(gzip.compress(f.read_bytes(), 6, mtime=0)).decode("ascii")  # mtime fixo: igual = igual
                # a data de cada ficheiro viaja com ele: os programas usam-na (pesquisa mais recente, «pesquisa de dd/mm»)
                datas[rel] = int(f.stat().st_mtime)
    return json.dumps({"versao": 1, "ficheiros": ficheiros, "datas": datas}, sort_keys=True)


def desempacotar(nome: str, texto: str | None) -> None:
    for pasta, _ in PACOTES[nome]:  # começa do zero: só fica o que está no pacote do Drive
        shutil.rmtree(pasta, ignore_errors=True)
        pasta.mkdir(parents=True, exist_ok=True)
    if not texto:
        return
    pacote = json.loads(texto)
    datas = pacote.get("datas") or {}
    for rel, dados in pacote["ficheiros"].items():
        destino = next(((p, rel[len(pre):]) for p, pre in sorted(PACOTES[nome], key=lambda x: -len(x[1])) if rel.startswith(pre)), None)
        if not destino:
            continue
        alvo = destino[0] / destino[1]
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(gzip.decompress(base64.b64decode(dados)))
        if rel in datas:
            os.utime(alvo, (datas[rel], datas[rel]))


def resumo(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# Relatórios: cópia do OneDrive que o Google Drive do PC faz; aqui ficam em cache entre voltas (só desce o que mudou)
# ---------------------------------------------------------------------------------------------------------------------
def sincronia_relatorios(lista: list[dict]) -> dict:
    """Compara os relatórios do PC (pc_relatorios.json, escrito pela ponte com o Cowork) com a cópia do Google Drive
    que a nuvem lê. Falta = está no PC e não na cópia; atrasado = a versão do PC é mais recente do que a da cópia."""
    try:
        pc = json.loads(ligacao("trab_lerFicheiros", {"pasta": "nuvem", "nomes": ["pc_relatorios.json"]}).get("pc_relatorios.json") or "{}")
    except (ValueError, RuntimeError):
        pc = {}
    if not pc.get("relatorios"):
        return {"estado": "sem_lista_do_pc"}
    drive = {x["caminho"]: x["mod"] / 1000 for x in lista}
    falta = sorted(c for c in pc["relatorios"] if c not in drive)
    atrasados = sorted(c for c, m in pc["relatorios"].items() if c in drive and m > drive[c] + 300)
    r = {"estado": "iguais" if not (falta or atrasados) else "diferentes", "pc": len(pc["relatorios"]), "nuvem": len(lista),
         "lista_pc_de": pc.get("quando"), "falta": falta[:20], "atrasados": atrasados[:20]}
    if falta or atrasados:
        registar(f"Relatórios por chegar à nuvem: {len(falta)} em falta, {len(atrasados)} desatualizados ({', '.join((falta + atrasados)[:5])}).")
    return r


def descarregar_relatorios() -> int:
    lista = ligacao("trab_relatorios")
    cache = CACHE / "relatorios"
    cache.mkdir(parents=True, exist_ok=True)
    for x in lista:  # nome do ficheiro em cache: o identificador do Drive e a data da última alteração
        x["cache"] = hashlib.sha1(str(x["id"]).encode("utf-8")).hexdigest() + f"_{x['mod']}"
    falta = [x for x in lista if not (cache / x["cache"]).exists()]
    lote, tam = [], 0
    for x in falta + [None]:
        if x is not None and tam + x["tam"] < 6_000_000 and len(lote) < 40:
            lote.append(x)
            tam += x["tam"]
            continue
        if lote:
            dados = ligacao("trab_lerBinarios", [y["id"] for y in lote])
            for y in lote:
                (cache / y["cache"]).write_bytes(base64.b64decode(dados[y["id"]]))
        lote, tam = ([x], x["tam"]) if x is not None else ([], 0)
    shutil.rmtree(PASTAS["relatorios"], ignore_errors=True)
    for x in lista:
        alvo = PASTAS["relatorios"] / x["caminho"]
        alvo.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache / x["cache"], alvo)
        # a data do ficheiro é a do relatório (a Análise usa-a quando o nome não traz data); sem isto, todos
        # apareciam com a data do dia em que a nuvem os descarregou
        os.utime(alvo, (x["mod"] / 1000, x["mod"] / 1000))
    for nome in ("CARTEIRA INVESTIMENTOS", "EM OBSERVAÇÃO", "ANALISES ANTIGAS"):
        (PASTAS["relatorios"] / nome).mkdir(parents=True, exist_ok=True)
    registar(f"Relatórios: {len(lista)} ({len(falta)} descarregados agora).")
    global SINCRONIA
    SINCRONIA = sincronia_relatorios(lista)
    return len(lista)


SINCRONIA: dict = {}


def aplicar_mudancas_pastas() -> None:
    """As mudanças de lista feitas na nuvem: a pasta muda aqui (cópia local) até o PC a mudar a sério no OneDrive."""
    if not MUDANCAS.exists():
        return
    mudancas, ficam = json.loads(MUDANCAS.read_text(encoding="utf-8")), []
    for m in mudancas:
        de, para = PASTAS["relatorios"] / m["de"].replace("\\", "/"), PASTAS["relatorios"] / m["para"].replace("\\", "/")
        if de.exists() and not para.exists():
            para.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(de), str(para))
            ficam.append(m)
        elif de.exists():
            ficam.append(m)  # as duas existem: o PC ainda não acabou; não se mexe
        # só existe o destino: o PC já a mudou, a mudança sai da lista
    MUDANCAS.write_text(json.dumps(ficam, ensure_ascii=False, indent=1), encoding="utf-8")
    if ficam:
        registar(f"Mudanças de pasta por fazer no PC: {len(ficam)}.")


# ---------------------------------------------------------------------------------------------------------------------
# Programas (os do PC, tal como estão)
# ---------------------------------------------------------------------------------------------------------------------
def ambiente() -> dict:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "CARTEIRA_EXPORTACAO": str(PASTAS["sync"] / "carteira_dados.json"),
            "CENTRO_PASTA_DRIVE": str(PASTAS["sync"]), "CENTRO_RELATORIOS": str(PASTAS["relatorios"]),
            "CENTRO_DESCOBERTA": str(DESCOBERTA / "resultados"), "CENTRO_PEDIDOS_COWORK": str(PASTAS["pedidos_cowork"]),
            "CENTRO_DADOS": str(PASTAS["centro_dados"]), "CENTRO_CARTEIRA_APP": str(RAIZ / "carteira"),
            "DECISAO_DIR": str(RAIZ / "decisao"), "SINC_PASTA": str(PASTAS["sync"]),
            "CENTRO_DECISAO_URL": f"http://127.0.0.1:{PORTAS['d']}",
            "XDG_CACHE_HOME": str(CACHE / "xdg")}


def arrancar() -> list[subprocess.Popen]:
    (RAIZ / "decisao" / "config.json").write_text(json.dumps({"pasta_relatorios": str(PASTAS["relatorios"]),
                                                              "pasta_dados": str(PASTAS["decisao_dados"])}), encoding="utf-8")
    env, procs = ambiente(), []
    for app, cwd, modulo in (("d", RAIZ / "decisao", "servidor.main:app"), ("c", RAIZ / "centro", "servidor:app")):
        log = open(TRAB / f"servidor_{app}.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", modulo, "--host", "127.0.0.1", "--port", str(PORTAS[app]),
                                       "--log-level", "warning"], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT))
    for app, caminho in (("d", "/api/listas"), ("c", "/api/estado")):
        for _ in range(90):
            try:
                pedir(app, "GET", caminho, timeout=30)
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        else:
            raise RuntimeError(f"O servidor {app} não arrancou: " + (TRAB / f"servidor_{app}.log").read_text(encoding="utf-8")[-2000:])
    registar("Programas a correr (Análise na 8600, Pesquisa na 8700).")
    return procs


def pedir(app: str, metodo: str, caminho: str, corpo=None, timeout: int = 600) -> tuple[int, object]:
    dados = None if corpo is None else json.dumps(corpo).encode("utf-8")
    pedido = urllib.request.Request(f"http://127.0.0.1:{PORTAS[app]}{caminho}", data=dados, method=metodo,
                                    headers={"Content-Type": "application/json"} if dados else {})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as r:
            estado, texto = r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        estado, texto = e.code, e.read().decode("utf-8", errors="replace")
    try:
        return estado, json.loads(texto) if texto else None
    except json.JSONDecodeError:
        return estado, {"detail": texto[:2000]}


# ---------------------------------------------------------------------------------------------------------------------
# Respostas: o que cada página mostra, agrupado por empresa (a mesma regra está no adaptador da app)
# ---------------------------------------------------------------------------------------------------------------------
def _limpo(t: str) -> str:
    return re.sub(r"[^A-Z0-9.\-]", "_", urllib.parse.unquote(t).upper())


def grupo_de(app: str, caminho: str) -> str:
    c = caminho.split("?")[0]
    if app == "d":
        m = re.match(r"^/api/(?:empresa|wiki|comparar)/([^/]+)", c)
    else:
        m = re.match(r"^/api/pesquisa/(?:criterios|analisar)/([^/]+)", c)
    return f"{app}_{_limpo(m.group(1))}" if m else f"{app}_global"


class Respostas:
    """Grupos lidos do Drive só quando são precisos; só os alterados voltam ao Drive."""

    def __init__(self):
        self.grupos: dict[str, dict] = {}
        self.alterados: set[str] = set()

    def _carregar(self, grupos: list[str]) -> None:
        falta = [g for g in grupos if g not in self.grupos]
        if falta:
            lidos = ligacao("trab_lerFicheiros", {"pasta": "respostas", "nomes": [g + ".json" for g in falta]})
            for g in falta:
                texto = lidos.get(g + ".json")
                self.grupos[g] = json.loads(texto) if texto else {"chaves": {}}

    def guardar(self, app: str, caminho: str, estado: int, corpo) -> str:
        g = grupo_de(app, caminho)
        self._carregar([g])
        self.grupos[g]["chaves"][caminho] = {"s": estado, "b": corpo, "t": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.alterados.add(g)
        return g

    def calcular(self, app: str, caminho: str) -> tuple[int, object]:
        inicio = time.time()
        estado, corpo = pedir(app, "GET", caminho)
        self.guardar(app, caminho, estado, corpo)
        if estado != 200:
            registar(f"  {caminho}: {estado} {str((corpo or {}).get('detail') if isinstance(corpo, dict) else '')[:150]}")
            if estado >= 500:  # erro do programa: a causa está no registo do servidor
                log = TRAB / f"servidor_{app}.log"
                for l in (log.read_text(encoding="utf-8", errors="replace").splitlines()[-6:] if log.exists() else []):
                    registar("    " + l[:200])
        elif time.time() - inicio > 20:
            registar(f"  {caminho}: {time.time() - inicio:.0f} s")
        return estado, corpo

    def enviar(self) -> None:
        if not self.alterados:
            return
        lote, tam = {}, 0
        for g in sorted(self.alterados):
            texto = json.dumps({**self.grupos[g], "gerado": datetime.now(timezone.utc).isoformat(timespec="seconds")}, ensure_ascii=False)
            if lote and tam + len(texto) > 8_000_000:
                ligacao("trab_gravarFicheiros", {"pasta": "respostas", "ficheiros": lote})
                lote, tam = {}, 0
            lote[g + ".json"] = texto
            tam += len(texto)
        if lote:
            ligacao("trab_gravarFicheiros", {"pasta": "respostas", "ficheiros": lote})
        registar(f"Respostas enviadas: {len(self.alterados)} grupo(s).")
        self.alterados.clear()


SINAIS_AGORA: dict[str, dict] = {}  # o sinal de cada empresa calculada nesta volta
DIARIO_ANTES: str | None = None     # o diário de decisões no início da volta (para saber se entrou um sinal novo)


def empresa(resp: Respostas, t: str) -> None:
    registar(f"Empresa {t}…")
    for parte in EMPRESA_D:
        estado, corpo = resp.calcular("d", f"/api/empresa/{t}/{parte}")
        if parte == "decisao" and estado == 200 and isinstance(corpo, dict) and (corpo.get("decisao") or {}).get("nivel"):
            dec = corpo["decisao"]
            SINAIS_AGORA[t.upper()] = {"nivel": dec["nivel"], "rotulo": dec.get("rotulo") or dec["nivel"].upper(), "nome": corpo.get("nome") or t}
    resp.calcular("d", f"/api/wiki/{t}")
    estado, tec = pedir("d", "GET", f"/api/empresa/{t}/tecnica")
    if estado == 200 and isinstance(tec, dict):
        resp.calcular("d", f"/api/empresa/{t}/compras?moeda={urllib.parse.quote(tec.get('moeda') or '', safe='')}")


def registar_sinais(resp: Respostas) -> None:
    """Compara o sinal de cada empresa da carteira e da observação com o da volta anterior. As mudanças ficam em
    sinais.json (nos dados da Análise: a lista de empresas marca-as durante 7 dias) e vão para o Telegram."""
    estado, listas = pedir("d", "GET", "/api/listas")
    if estado != 200 or not SINAIS_AGORA:
        return
    seguidas_ = {x["ticker"].upper() for k in ("carteira", "observacao") for x in (listas.get("listas") or {}).get(k, [])}
    ficheiro = PASTAS["decisao_dados"] / "sinais.json"
    try:
        s = json.loads(ficheiro.read_text(encoding="utf-8")) if ficheiro.exists() else {}
    except ValueError:
        s = {}
    atual, mudancas, novas = s.get("atual", {}), s.get("mudancas", []), []
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")  # hora de Lisboa (TZ do fluxo)
    for t, x in SINAIS_AGORA.items():
        if t not in seguidas_:
            continue
        antes = atual.get(t) or {}
        if antes.get("nivel") and antes["nivel"] != x["nivel"]:
            novas.append({"ticker": t, "nome": x["nome"], "de": antes.get("rotulo") or antes["nivel"], "para": x["rotulo"],
                          "de_nivel": antes["nivel"], "para_nivel": x["nivel"], "quando": agora})
        atual[t] = {"nivel": x["nivel"], "rotulo": x["rotulo"],
                    "desde": antes.get("desde") if antes.get("nivel") == x["nivel"] else agora}
    limite = datetime.fromtimestamp(time.time() - 30 * 86400).strftime("%Y-%m-%d %H:%M")
    mudancas = [m for m in mudancas + novas if m["quando"] >= limite][-100:]
    ficheiro.write_text(json.dumps({"atual": atual, "mudancas": mudancas}, ensure_ascii=False), encoding="utf-8")
    if not novas:
        return
    registar("Sinais mudados: " + ", ".join(f"{m['ticker']} {m['de']} -> {m['para']}" for m in novas))
    resp.calcular("d", "/api/listas")  # a lista de empresas passa a mostrar a marca
    texto = "🔄 *Sinal mudou*\n" + "\n".join(f"*{m['ticker']}*: {m['de']} → {m['para']}" for m in novas)
    try:
        ligacao("trab_avisar", texto)
    except Exception as e:  # noqa: BLE001  (o aviso é um extra: não pode estragar a volta)
        registar(f"Aviso no Telegram falhou: {e}")


def seguidas() -> list[str]:
    estado, listas = pedir("d", "GET", "/api/listas")
    tickers = [x["ticker"] for k in ("carteira", "observacao") for x in ((listas or {}).get("listas") or {}).get(k, [])] if estado == 200 else []
    estado, pd = pedir("d", "GET", "/api/por_decidir")
    tickers += [x["ticker"] for x in (pd or [])] if estado == 200 else []
    if VISTOS.exists():
        limite = time.time() - 30 * 86400
        tickers += [t for t, quando in json.loads(VISTOS.read_text(encoding="utf-8")).items() if quando > limite]
    return list(dict.fromkeys(t.upper() for t in tickers))


def marcar_visto(t: str) -> None:
    vistos = json.loads(VISTOS.read_text(encoding="utf-8")) if VISTOS.exists() else {}
    vistos[t.upper()] = time.time()
    VISTOS.write_text(json.dumps(vistos), encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# Pedidos da app
# ---------------------------------------------------------------------------------------------------------------------
def correr_pesquisas(ident: str | None = None) -> tuple[int, list[str]]:
    args = [sys.executable, "-u", "automatico.py"] + (["--pesquisa", ident] if ident else ["--todas"]) + ["--sem-notificacao"]
    registar("Pesquisa automática: " + (ident or "todas") + "…")
    r = subprocess.run(args, cwd=DESCOBERTA, env=ambiente(), capture_output=True, text=True, encoding="utf-8", errors="replace")
    linhas = (r.stdout + r.stderr).strip().splitlines()[-12:]
    for l in linhas[-4:]:
        registar("  " + l)
    return r.returncode, linhas


def tratar_pedidos(resp: Respostas) -> tuple[list[dict], set[str], bool]:
    pedidos = ligacao("trab_pedidos") or []
    feitos, tocadas, escreveu, feitas = [], set(), False, set()
    for p in pedidos:
        app, metodo, caminho = p["app"], p["metodo"], p["caminho"]
        m = re.search(r"/api/(?:empresa|wiki|renovar|por_decidir|pesquisa/criterios|pesquisa/analisar)/([^/?]+)", caminho)
        if m:
            tocadas.add(urllib.parse.unquote(m.group(1)).upper())
        if isinstance(p.get("corpo"), dict) and p["corpo"].get("ticker"):
            tocadas.add(str(p["corpo"]["ticker"]).upper())
        try:
            if metodo == "GET":
                aberta = re.match(r"^/api/(?:empresa|wiki)/([^/?]+)", caminho) if app == "d" else None
                if aberta:  # empresa aberta na app: calculam-se logo todos os separadores dela, não só o pedido
                    t = urllib.parse.unquote(aberta.group(1)).upper()
                    marcar_visto(t)
                    if t not in feitas:
                        empresa(resp, t)
                        feitas.add(t)
                estado, corpo = resp.calcular(app, caminho)
                feitos.append({"id": p["id"], "estado": "feito", "resultado": {"s": estado, "grupo": grupo_de(app, caminho)}})
                continue
            escreveu = True
            pm = re.match(r"^/api/pesquisas/([a-z0-9\-]+)/correr$", caminho)
            if app == "c" and metodo == "POST" and pm:
                codigo, linhas = correr_pesquisas(None if pm.group(1) == "todas" else pm.group(1))
                estado, corpo = (200 if codigo == 0 else 500), {"codigo": codigo, "registo": linhas}
            else:
                estado, corpo = pedir(app, metodo, caminho, p.get("corpo"))
            if app == "d" and metodo == "POST" and caminho.startswith("/api/listas/") and estado == 200 and (corpo or {}).get("movida"):
                mud = json.loads(MUDANCAS.read_text(encoding="utf-8")) if MUDANCAS.exists() else []
                mud.append({**corpo["movida"], "quando": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                MUDANCAS.write_text(json.dumps(mud, ensure_ascii=False, indent=1), encoding="utf-8")
            registar(f"Pedido {metodo} {caminho}: {estado}")
            feitos.append({"id": p["id"], "estado": "feito" if estado < 400 else "erro", "resultado": {"s": estado, "b": corpo}})
        except Exception as e:  # noqa: BLE001 — um pedido falhado não pára os outros
            registar(f"Pedido {metodo} {caminho} falhou: {e}")
            feitos.append({"id": p["id"], "estado": "erro", "resultado": {"s": 500, "b": {"detail": str(e)[:500]}}})
    return feitos, tocadas - feitas, escreveu


# ---------------------------------------------------------------------------------------------------------------------
# Uma volta
# ---------------------------------------------------------------------------------------------------------------------
def sincronizar_carteira(tudo: bool) -> None:
    """O que o PC fazia para a Carteira no Google (painel da Decisão, técnica rápida, triagens e listas pedidas na
    Carteira), agora pela Análise que já está a correr: o mesmo sincronizar.py, no modo nuvem."""
    env = {**ambiente(), "SINC_NA_NUVEM": "1", "SINC_DECISAO_URL": f"http://127.0.0.1:{PORTAS['d']}",
           "SINC_SAIDA": str(PASTAS["sync"] / "sincronizacao_carteira.json"),
           "SINC_ESTADO": str(PASTAS["centro_dados"] / "estado_sincronizacao.json"), "SINC_MUDANCAS": str(MUDANCAS)}
    r = subprocess.run([sys.executable, "-u", str(RAIZ / "carteira" / "ferramentas" / "sincronizar.py")] + (["--tudo"] if tudo else []),
                       cwd=RAIZ / "carteira" / "ferramentas", env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    for l in [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][-8:]:
        registar("  Carteira: " + l[-200:])


def guardar_dados(originais: dict) -> None:
    """Os pacotes de dados que mudaram voltam ao Drive (e passam a ser a referência para a próxima comparação)."""
    novos = {}
    for nome in PACOTES:
        texto = empacotar(nome)
        if resumo(texto) != originais[nome]:
            novos[nome] = texto
            originais[nome] = resumo(texto)
    if novos:
        ligacao("trab_gravarFicheiros", {"pasta": "nuvem", "ficheiros": novos})
        registar("Dados gravados no Drive: " + ", ".join(novos))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tudo", action="store_true")
    ap.add_argument("--pesquisa", action="store_true")
    args = ap.parse_args()
    if not (LIGACAO and TOKEN and EXECUCAO):
        print("Faltam LIGACAO_URL, GITHUB_TOKEN ou GITHUB_RUN_ID.")
        return 2
    inicio = datetime.now(timezone.utc)
    TRAB.mkdir(parents=True, exist_ok=True)
    for p in PASTAS.values():
        p.mkdir(parents=True, exist_ok=True)

    # 1. Dados, relatórios e sincronização da Carteira
    pacotes = ligacao("trab_lerFicheiros", {"pasta": "nuvem", "nomes": list(PACOTES) + ["cowork_estado.json"]})
    for nome in PACOTES:
        desempacotar(nome, pacotes.get(nome))
    # O estado dos pedidos ao Cowork que o PC devolveu (o Cowork escreve-o nos ficheiros de _PEDIDOS): o do PC manda
    for nome, texto in json.loads(pacotes.get("cowork_estado.json") or "{}").items():
        if re.fullmatch(r"[\w.\-]+\.json", nome):
            (PASTAS["pedidos_cowork"] / nome).write_text(texto, encoding="utf-8")
    originais = {nome: resumo(empacotar(nome)) for nome in PACOTES}
    global DIARIO_ANTES
    f_diario = PASTAS["decisao_dados"] / "diario.json"
    DIARIO_ANTES = f_diario.read_text(encoding="utf-8") if f_diario.exists() else None
    sync = ligacao("trab_lerFicheiros", {"pasta": "sync", "nomes": FICHEIROS_SYNC})
    for nome, texto in sync.items():
        if texto:
            (PASTAS["sync"] / nome).write_text(texto, encoding="utf-8")
    descarregar_relatorios()
    aplicar_mudancas_pastas()

    procs = arrancar()
    resp = Respostas()
    erro = None
    feitos: list[dict] = []
    try:
        if args.pesquisa:
            correr_pesquisas()
        # 2. Pedidos da app, com as respostas gerais e as das empresas tocadas; a app recebe-os logo
        feitos, tocadas, escreveu = tratar_pedidos(resp)
        for c in GLOBAIS_D:
            resp.calcular("d", c)
        for c in GLOBAIS_C:
            resp.calcular("c", c)
        for t in tocadas:
            try:
                empresa(resp, t)
            except Exception as e:  # noqa: BLE001
                registar(f"Empresa {t} falhou: {e}")
        resp.enviar()
        guardar_dados(originais)
        if feitos:
            ligacao("trab_concluirPedidos", feitos)
            feitos = []
        # 3. Voltas agendadas: todas as empresas seguidas
        if args.tudo or args.pesquisa:
            for t in [t for t in seguidas() if t not in tocadas]:
                try:
                    empresa(resp, t)
                except Exception as e:  # noqa: BLE001
                    registar(f"Empresa {t} falhou: {e}")
                if len(resp.alterados) >= 12:
                    resp.enviar()  # vai enviando: a app vê as primeiras empresas sem esperar pelas últimas
        registar_sinais(resp)
        # Candidatas (sem rede: lê o resumo de cada decisão) sempre que se calculou alguma empresa; o Diário (vai ao Yahoo
        # buscar os preços de todas) nas voltas completas e quando entrou um sinal novo
        if SINAIS_AGORA:
            resp.calcular("d", "/api/candidatas")
        diario = PASTAS["decisao_dados"] / "diario.json"
        if args.tudo or args.pesquisa or (diario.read_text(encoding="utf-8") if diario.exists() else None) != DIARIO_ANTES:
            resp.calcular("d", "/api/diario")
        resp.enviar()
        # 4. A Carteira no Google (painel da Decisão, técnica rápida, triagens e listas pedidas lá)
        antes = {n: (PASTAS["sync"] / n).read_text(encoding="utf-8") if (PASTAS["sync"] / n).exists() else None for n in FICHEIROS_SYNC}
        sincronizar_carteira(args.tudo)
        mudou = {n: (PASTAS["sync"] / n).read_text(encoding="utf-8") for n in FICHEIROS_SYNC
                 if n != "pedidos_carteira.json" and (PASTAS["sync"] / n).exists() and (PASTAS["sync"] / n).read_text(encoding="utf-8") != antes[n]}
        # Proteção: um ficheiro com menos empresas do que o anterior (técnica vazia, sincronização sem análises) não
        # substitui o que a Carteira está a mostrar
        for n, chave in (("tecnica_carteira.json", "ativos"), ("sincronizacao_carteira.json", "decisao")):
            if n in mudou and antes.get(n):
                try:
                    novo, velho = json.loads(mudou[n]).get(chave) or {}, json.loads(antes[n]).get(chave) or {}
                    tam = (lambda x: len(x.get("analises", x)) if isinstance(x, dict) else 0)
                    if tam(velho) and tam(novo) < tam(velho) * 0.5:
                        registar(f"Carteira: {n} novo com {tam(novo)} empresas (antes {tam(velho)}): não enviado.")
                        mudou.pop(n)
                except (ValueError, AttributeError):
                    pass
        if mudou:
            ligacao("trab_gravarFicheiros", {"pasta": "sync", "ficheiros": mudou})
            registar("Carteira: enviados " + ", ".join(mudou))
            if escreveu or len(mudou) > 0:
                resp.calcular("c", "/api/decisao/resumo")
                resp.enviar()
    except Exception as e:  # noqa: BLE001
        erro = f"{type(e).__name__}: {e}"
        registar("ERRO: " + erro)
    finally:
        # 5. Dados alterados de volta ao Drive; pedidos que faltem concluir; estado da volta
        guardar_dados(originais)
        if feitos:
            ligacao("trab_concluirPedidos", feitos)
        ligacao("trab_gravarFicheiros", {"pasta": "nuvem", "ficheiros": {"estado.json": json.dumps({
            "inicio": inicio.isoformat(timespec="seconds"), "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "motivo": os.environ.get("MOTIVO") or ("pesquisa" if args.pesquisa else "agendada" if args.tudo else "pedido"),
            "completa": bool(args.tudo or args.pesquisa), "pedidos": len(feitos), "relatorios": SINCRONIA,
            "erro": erro, "registo": REGISTO[-60:]}, ensure_ascii=False)}})
        for p in procs:
            p.terminate()
    return 1 if erro else 0


if __name__ == "__main__":
    sys.exit(main())
