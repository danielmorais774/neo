"""A configuração da transcrição que o neo-panel grava (Configurações → Transcrição).

Três coisas, todas no ``user_preference`` global (service="neo",
category="transcricao"):

- **a chave do Groq**, cifrada com a ``NEO_CRED_KEY`` (o mesmo Fernet do cofre e
  do catálogo de LLM). O painel só vê a origem e os últimos 4 caracteres. Sem
  chave gravada, vale a ``GROQ_API_KEY`` do ``.env``. A chave NÃO vai para o
  cofre (``credential``): o ``vault_list`` mostra todo item do cofre ao cérebro;
- **"só a cota gratuita"** (padrão: ligado). Ligado, a tool para antes de passar
  dos limites do plano gratuito (``[transcricao.cota]`` no ``neo.toml``);
- **o modelo** do Groq (padrão: ``whisper-large-v3-turbo``).

O service "neo" fica fora do prompt de propósito: só as preferências de
"neo-desktop" entram no contexto do cérebro (``get_preferences_text``).

A leitura é síncrona (quem monta o toolset não é async), então o módulo guarda o
último valor conhecido: ``carregar`` lê do banco no boot e cada gravação atualiza
a memória na hora, sem reiniciar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

PREF_SERVICE = "neo"
PREF_CATEGORY = "transcricao"
KEY_CHAVE = "groq_api_key_cifrada"
KEY_SO_GRATUITA = "so_cota_gratuita"
KEY_MODELO = "modelo"

MODELO_PADRAO = "whisper-large-v3-turbo"


class ConfigError(Exception):
    """Erro de gravação com a mensagem que o painel mostra."""


@dataclass(frozen=True)
class _Estado:
    chave_cifrada: str | None = None
    so_gratuita: bool | None = None
    modelo: str | None = None


_estado = _Estado()
#: A chave gravada não decifra (NEO_CRED_KEY trocada ou ausente).
_chave_quebrada = False


def _fernet() -> Any:
    from shared.config import get_settings

    key = getattr(get_settings(), "neo_cred_key", "") or ""
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


def _chave_do_env() -> str:
    from shared.config import get_settings

    return str(getattr(get_settings(), "groq_api_key", "") or "").strip()


def _chave_do_painel() -> str:
    global _chave_quebrada
    if not _estado.chave_cifrada:
        return ""
    fernet = _fernet()
    if fernet is None:
        _chave_quebrada = True
        return ""
    try:
        valor = fernet.decrypt(_estado.chave_cifrada.encode()).decode()
    except Exception:  # noqa: BLE001 — InvalidToken: a NEO_CRED_KEY mudou
        _chave_quebrada = True
        return ""
    _chave_quebrada = False
    return valor


def chave() -> str:
    """A chave em uso: a do painel; sem ela, a do ``.env``; sem as duas, ``""``."""
    return _chave_do_painel() or _chave_do_env()


def origem_da_chave() -> str:
    """``painel``, ``env`` ou ``nenhuma``."""
    if _chave_do_painel():
        return "painel"
    if _chave_do_env():
        return "env"
    return "nenhuma"


def chave_quebrada() -> bool:
    """Há chave gravada no painel, mas ela não decifra."""
    _chave_do_painel()
    return bool(_estado.chave_cifrada) and _chave_quebrada


def final_da_chave() -> str:
    valor = chave()
    return valor[-4:] if len(valor) >= 8 else ""


def cofre_disponivel() -> bool:
    """Sem ``NEO_CRED_KEY`` não dá para gravar chave pelo painel."""
    return _fernet() is not None


def so_gratuita() -> bool:
    return True if _estado.so_gratuita is None else bool(_estado.so_gratuita)


def modelo() -> str:
    return _estado.modelo or MODELO_PADRAO


def disponivel() -> bool:
    """A tool tem como rodar? (há uma chave em uso)."""
    return bool(chave())


def _bool(valor: Any) -> bool:
    return str(valor).strip().lower() in ("true", "1", "on", "sim")


async def carregar(db: Any) -> None:
    """Lê o que o painel gravou. Banco fora: fica o que estava."""
    global _estado
    try:
        prefs = await db.get_preferences(user_id=None, service=PREF_SERVICE, category=PREF_CATEGORY)
    except Exception as e:  # noqa: BLE001 — a configuração nunca derruba o boot
        logger.warning("[transcricao] não li a configuração do painel: %s", e)
        return
    valores = {getattr(p, "key", None): getattr(p, "value", None) for p in prefs or []}
    _estado = _Estado(
        chave_cifrada=valores.get(KEY_CHAVE) or None,
        so_gratuita=_bool(valores[KEY_SO_GRATUITA]) if KEY_SO_GRATUITA in valores else None,
        modelo=(str(valores.get(KEY_MODELO) or "").strip() or None),
    )
    if chave_quebrada():
        logger.warning(
            "[transcricao] a chave do Groq gravada no painel não decifra (NEO_CRED_KEY mudou?) — "
            "usando a do .env, se houver"
        )


async def _upsert(db: Any, key: str, value: str) -> None:
    await db.upsert_preference(
        category=PREF_CATEGORY,
        key=key,
        value=value,
        user_id=None,
        service=PREF_SERVICE,
        source_message="neo-panel settings",
    )


async def gravar_chave(db: Any, valor: str) -> None:
    global _estado
    limpa = (valor or "").strip()
    if not limpa:
        raise ConfigError("a chave está vazia.")
    fernet = _fernet()
    if fernet is None:
        raise ConfigError("o cofre está desligado (NEO_CRED_KEY ausente): não há como guardar a chave cifrada.")
    cifrada = fernet.encrypt(limpa.encode()).decode()
    await _upsert(db, KEY_CHAVE, cifrada)
    _estado = replace(_estado, chave_cifrada=cifrada)
    logger.info("[transcricao] chave do Groq gravada pelo painel (…%s)", limpa[-4:])


async def apagar_chave(db: Any) -> None:
    global _estado
    await db.delete_preference(
        category=PREF_CATEGORY, key=KEY_CHAVE, user_id=None, service=PREF_SERVICE
    )
    _estado = replace(_estado, chave_cifrada=None)
    logger.info("[transcricao] chave do Groq removida do painel")


async def gravar_opcoes(db: Any, *, so_gratuita: bool | None = None, modelo: str | None = None) -> None:
    global _estado
    if so_gratuita is not None:
        await _upsert(db, KEY_SO_GRATUITA, "true" if so_gratuita else "false")
        _estado = replace(_estado, so_gratuita=bool(so_gratuita))
        logger.info("[transcricao] só a cota gratuita: %s", "LIGADO" if so_gratuita else "DESLIGADO")
    if modelo is not None:
        limpo = modelo.strip()
        if not limpo:
            raise ConfigError("o modelo está vazio.")
        await _upsert(db, KEY_MODELO, limpo)
        _estado = replace(_estado, modelo=limpo)
        logger.info("[transcricao] modelo: %s", limpo)


def _esquecer() -> None:
    """Só para teste: volta aos padrões."""
    global _estado, _chave_quebrada
    _estado = _Estado()
    _chave_quebrada = False
