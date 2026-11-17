"""Hash de senha (api/auth/passwords.py)."""

from __future__ import annotations

import hashlib

from api.auth import passwords


def test_hash_tem_salt_por_conta():
    """Duas contas com a MESMA senha não podem ter o mesmo hash."""
    a = passwords.hash_password("mesma-senha")
    b = passwords.hash_password("mesma-senha")
    assert a != b
    assert passwords.verify_password("mesma-senha", a)
    assert passwords.verify_password("mesma-senha", b)


def test_senha_errada_nao_passa():
    assert not passwords.verify_password("outra", passwords.hash_password("certa"))


def test_formato_legado_e_reconhecido_e_marcado_para_migrar():
    legado = hashlib.sha256(b"neo-salt123456").hexdigest()
    assert passwords.verify_password("123456", legado)
    assert passwords.needs_rehash(legado)
    assert not passwords.needs_rehash(passwords.hash_password("123456"))


def test_hash_vazio_nao_autentica_ninguem():
    assert not passwords.verify_password("", None)
    assert not passwords.verify_password("qualquer", "")


def test_iteracoes_menores_pedem_rehash():
    fraco = passwords.hash_password("x", iterations=1000)
    assert passwords.verify_password("x", fraco)
    assert passwords.needs_rehash(fraco)
