"""
Tradução para português de Portugal (pt-PT), com cache em disco (traducoes.json na pasta de dados): cada texto só é
traduzido uma vez. Usa o serviço gratuito do Google Tradutor; se falhar, devolve None e a app mostra o original.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
import urllib.request

import config

FICHEIRO = "traducoes.json"
_cadeado = threading.Lock()
_cache: dict | None = None
MAX_BLOCO = 1500  # carateres por pedido (o serviço aceita pedidos por GET, com limite de tamanho)

# Correções de termos que o tradutor por vezes deixa em português do Brasil
PT_PT = [
    (r"\busuários\b", "utilizadores"), (r"\busuário\b", "utilizador"), (r"\busuárias?\b", "utilizadora"),
    (r"\bequipe(s?)\b", r"equipa\1"), (r"\bcelulares\b", "telemóveis"), (r"\bcelular\b", "telemóvel"),
    (r"\barquivo(s?)\b", r"ficheiro\1"), (r"\b(n|d)?as telas\b", r"\1os ecrãs"), (r"\b(n|d)?a tela\b", r"\1o ecrã"),
    (r"\btelas\b", "ecrãs"), (r"\btela\b", "ecrã"), (r"\bônibus\b", "autocarro"), (r"\bgerenciamento\b", "gestão"),
    (r"\bgerenciar\b", "gerir"), (r"\bcontato(s?)\b", r"contacto\1"), (r"\bfato(s?)\b", r"facto\1"), (r"\bregistro(s?)\b", r"registo\1"),
    (r"\bplanejamento\b", "planeamento"), (r"\bpresidente-executivo\b", "presidente executivo"),
]


def _carregar() -> dict:
    global _cache
    if _cache is None:
        _cache = config.ler_json(FICHEIRO, {})
    return _cache


def _chave(texto: str) -> str:
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()


def _blocos(texto: str) -> list[str]:
    frases = re.split(r"(?<=[.!?])\s+", texto)
    blocos, atual = [], ""
    for f in frases:
        if len(atual) + len(f) + 1 > MAX_BLOCO and atual:
            blocos.append(atual)
            atual = f
        else:
            atual = f"{atual} {f}".strip()
    if atual:
        blocos.append(atual)
    return blocos


def _pedir(texto: str) -> str:
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "pt-PT", "dt": "t", "q": texto})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
        dados = json.loads(r.read())
    return "".join(s[0] for s in dados[0] if s and s[0])


def corrigir_pt_pt(texto: str) -> str:
    """Troca termos do Brasil pelos de Portugal, mantendo a maiúscula inicial ("Equipe" -> "Equipa")."""
    for padrao, subst in PT_PT:
        def trocar(m, subst=subst):
            novo = m.expand(subst)
            return novo[:1].upper() + novo[1:] if m.group(0)[:1].isupper() else novo
        texto = re.sub(padrao, trocar, texto, flags=re.IGNORECASE)
    return texto


def traduzir(texto: str | None) -> str | None:
    """Texto em pt-PT, ou None se não for possível traduzir agora."""
    if not texto or not texto.strip():
        return texto
    k = _chave(texto)
    with _cadeado:
        cache = _carregar()
        if k in cache:
            return cache[k]
    try:
        traduzido = corrigir_pt_pt(" ".join(_pedir(b) for b in _blocos(texto)))
    except Exception:  # noqa: BLE001  (sem rede ou serviço indisponível: mostra-se o original)
        return None
    with _cadeado:
        cache = _carregar()
        cache[k] = traduzido
        config.escrever_json(FICHEIRO, cache)
    return traduzido
