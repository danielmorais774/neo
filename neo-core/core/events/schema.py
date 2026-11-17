"""O contrato do ``eventos:`` do SKILL.md (docs/playlists-skill-plan.md §7.6).

Uma skill declara o que fazer quando um evento chega, e a receita é **nomeada,
não programada**: ``faz:`` só aceita um runner que existe em
:mod:`core.events.runners`. Isso é o que mantém a fase 4 fora do território de
"skill executa código arbitrário dentro do neo-core" (a F8 do plano de datasets,
que continua atrás de flag e com humano na frente).

O mesmo objeto alimenta três consumidores: a validação na leitura do SKILL.md, o
``event_subscribe`` (que checa se o gatilho existe antes de gravar inscrição) e o
runner na hora do disparo.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

#: Nome de gatilho/canal — mesmo alfabeto dos identificadores de cadastro.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,32}$")

#: Eventos que o barramento conhece hoje. Fechado de propósito: um nome errado
#: no SKILL.md vira inscrição que nunca dispara, e isso é caríssimo de debugar.
KNOWN_EVENTS = ("faixa_iniciou", "faixa_terminando", "faixa_terminou", "faixa_pulada")

#: Eventos que fazem a fila ANDAR. Os demais (``faixa_iniciou``) só atualizam
#: estado — e por isso não passam pela dedup nem re-armam a inscrição: se
#: passassem, o "começou a tocar" comeria a vez do "está acabando" da mesma
#: faixa (mesma chave de dedup) e a fila travaria na primeira música.
ADVANCING_EVENTS = ("faixa_terminando", "faixa_terminou", "faixa_pulada")

#: Canais (superfícies). "musica" é o único com transporte implementado.
KNOWN_CHANNELS = ("musica",)


class TriggerError(ValueError):
    """Gatilho inválido — vira aviso no load da skill e erro no subscribe."""


class EventTrigger(BaseModel):
    """Um gatilho declarado em ``eventos:``."""

    #: Identificador dentro da skill. O par ``<skill>.<nome>`` é o `handler_ref`
    #: gravado na inscrição.
    nome: str
    #: Eventos que acionam este gatilho.
    quando: list[str] = Field(default_factory=list)
    canal: str = "musica"
    #: Runner do core que executa a reação.
    faz: str

    # --- configuração do runner `proxima_da_fila` --------------------------
    #: Slug do cadastro que guarda a fila.
    fila: str = ""
    ordem: str = "posicao asc"
    #: Filtro estruturado das linhas pendentes ({"estado": "pendente"}).
    filtro: dict[str, Any] = Field(default_factory=dict)
    campo_link: str = "link"
    campo_titulo: str = ""
    campo_estado: str = ""
    #: Campo que guarda o CÓDIGO único do item despachado. É ele que o aparelho
    #: devolve em cada evento, e é ele que faz "marcar quem começou a tocar"
    #: acertar a linha certa mesmo se a fila mudou no meio do caminho.
    campo_codigo: str = ""
    #: {"despachada": "tocando", "anterior": "tocada"} — os valores que o runner
    #: grava no `campo_estado` ao avançar. Vazio = não marca nada (a fila vira
    #: um loop infinito na mesma linha, então o runner recusa).
    marca: dict[str, str] = Field(default_factory=dict)
    #: "fila_vazia" (padrão) ou "nunca".
    encerra_quando: str = "fila_vazia"
    #: Texto opcional avisado no chat quando a fila acaba.
    aviso_final: str = ""

    @field_validator("nome", "canal", "faz")
    @classmethod
    def _check_ident(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if not _NAME_RE.match(value):
            raise TriggerError(
                f"identificador inválido: {v!r} (minúsculas, números e underscore)."
            )
        return value

    @model_validator(mode="after")
    def _check(self) -> "EventTrigger":
        from core.events.runners import RUNNERS

        if self.canal not in KNOWN_CHANNELS:
            raise TriggerError(
                f"canal desconhecido: '{self.canal}'. Canais: {', '.join(KNOWN_CHANNELS)}."
            )
        eventos = [str(e).strip().lower() for e in (self.quando or []) if str(e).strip()]
        if not eventos:
            raise TriggerError("'quando' precisa de pelo menos um evento.")
        desconhecidos = [e for e in eventos if e not in KNOWN_EVENTS]
        if desconhecidos:
            raise TriggerError(
                f"evento(s) desconhecido(s): {', '.join(desconhecidos)}. "
                f"Eventos: {', '.join(KNOWN_EVENTS)}."
            )
        self.quando = eventos
        if self.faz not in RUNNERS:
            raise TriggerError(
                f"'faz' desconhecido: '{self.faz}'. Disponíveis: {', '.join(sorted(RUNNERS))}."
            )
        if self.faz == "proxima_da_fila":
            if not self.fila:
                raise TriggerError("'proxima_da_fila' exige 'fila' (o slug do cadastro).")
            if not self.campo_estado or not self.marca.get("despachada"):
                raise TriggerError(
                    "'proxima_da_fila' exige 'campo_estado' e marca.despachada — sem marcar "
                    "a linha despachada a fila repetiria a mesma faixa para sempre."
                )
            if "faixa_iniciou" in self.quando and not self.marca.get("tocando"):
                raise TriggerError(
                    "quem escuta 'faixa_iniciou' precisa de marca.tocando — é o estado "
                    "que separa 'mandei tocar' de 'está tocando de verdade'."
                )
        if self.encerra_quando not in ("fila_vazia", "nunca"):
            raise TriggerError(
                f"'encerra_quando' inválido: {self.encerra_quando!r} (use fila_vazia ou nunca)."
            )
        return self

    def accepts(self, event: str) -> bool:
        return (event or "").strip().lower() in self.quando


def parse_triggers(raw: Any, *, skill: str = "") -> list[EventTrigger]:
    """Lê o ``eventos:`` de um frontmatter. Erro traz o nome da skill junto."""
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise TriggerError("'eventos' precisa ser uma lista.")
    out: list[EventTrigger] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TriggerError(f"entrada inválida em 'eventos': {item!r} — esperado um objeto.")
        data = dict(item)
        quando = data.get("quando")
        if isinstance(quando, str):
            data["quando"] = [quando]
        try:
            out.append(EventTrigger.model_validate(data))
        except TriggerError:
            raise
        except Exception as exc:  # noqa: BLE001 — ValidationError vira frase
            raise TriggerError(f"gatilho inválido em '{skill or '?'}': {exc}") from exc
    nomes = [t.nome for t in out]
    dupes = {n for n in nomes if nomes.count(n) > 1}
    if dupes:
        raise TriggerError(f"gatilhos repetidos: {', '.join(sorted(dupes))}.")
    return out
