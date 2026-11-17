"""Hash de senha com a stdlib — PBKDF2-HMAC-SHA256.

O hash que existia (``db/seeds.py``, ``sha256("neo-salt" + senha)``) tem salt
FIXO e uma única rodada: duas contas com a mesma senha dão o mesmo hash, e uma
GPU testa bilhões por segundo. Migrar exige senha em claro, que só existe no
login — então o formato legado é aceito **uma última vez** e a linha é
re-hasheada ali (``needs_rehash``).

``hashlib.pbkdf2_hmac`` é stdlib de propósito: passlib/bcrypt/argon2 seriam
melhores por design, mas nenhum está no ``pyproject.toml`` e o ganho real sobre
PBKDF2 com 600k iterações não paga uma dependência nova numa imagem que já
demora a subir.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

#: Iterações do PBKDF2. Referência: o mínimo recomendado pelo OWASP para
#: PBKDF2-HMAC-SHA256 em 2023 era 600k. Custa ~0,2s por login, e login é raro.
ITERATIONS = 600_000
ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16

#: Salt fixo do formato legado (``db/seeds.py``). Mantido só para reconhecer e
#: migrar o hash antigo; nada novo é escrito neste formato.
_LEGACY_SALT = "neo-salt"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Devolve ``pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>``."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGO}${iterations}${_b64(salt)}${_b64(digest)}"


def _verify_legacy(password: str, stored: str) -> bool:
    legacy = hashlib.sha256(f"{_LEGACY_SALT}{password}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored)


def verify_password(password: str, stored: str | None) -> bool:
    """Confere a senha contra o hash guardado. Aceita o formato legado.

    ``stored`` vazio/None devolve ``False`` **gastando o mesmo tempo** de um hash
    real: sem isso, "usuário sem senha" e "senha errada" têm latências
    diferentes e viram enumeração de conta.
    """
    if not stored:
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"\x00" * _SALT_BYTES, ITERATIONS)
        return False

    if stored.startswith(f"{ALGO}$"):
        try:
            _algo, iters, salt_b64, hash_b64 = stored.split("$", 3)
            iterations = int(iters)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
        except (ValueError, TypeError):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(digest, expected)

    # Formato legado (hex de 64 chars, sha256 com salt fixo).
    if len(stored) == 64:
        return _verify_legacy(password, stored)
    return False


def needs_rehash(stored: str | None) -> bool:
    """Verdadeiro quando o hash guardado é legado ou tem iterações a menos."""
    if not stored:
        return False
    if not stored.startswith(f"{ALGO}$"):
        return True
    try:
        iterations = int(stored.split("$", 2)[1])
    except (ValueError, IndexError):
        return True
    return iterations < ITERATIONS
