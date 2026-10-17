"""Versão do trabalhador da nuvem: sem acesso direto à folha (na nuvem quem lê e grava é a ligação Apps Script)."""
ID_FOLHA = ''
ESCRITAS: dict = {}


def disponivel() -> bool:
    return False


def email_conta():
    return None


def ler(intervalos):
    raise RuntimeError('sem acesso à folha na nuvem')


def ler_dados():
    raise RuntimeError('sem acesso à folha na nuvem')
