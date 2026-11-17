"""Corpo local: o Claude Code do Mac do Daniel, pelo canal ``/ws/devices/control``.

Mesmo cano do navegador local. O desktop (``services/claude_cli.rs``) sobe o
``claude`` destacado, com o login que o Daniel já fez — o Neo não toca em
credencial (docs/claude-code-delegation-plan.md §3.1 e §4).

Permissões: ``auto`` por padrão (o classificador da Anthropic deixa passar o
que é seguro, como rodar o script da própria pasta) e ``--permission-prompts
none``: o que ele ainda barraria é negado na hora e volta em
``permission_denials``, e quem pergunta ao Daniel é o orquestrador. O ``auto``
julga RISCO, não lugar (medido na F0: escreveu em ``/tmp``) — por isso existe
``acceptEdits`` como opção, que só libera editar dentro da pasta e pergunta todo
comando (job decc8224: até ``python3 hello.py`` virava pergunta).
"""

from __future__ import annotations

import logging
from typing import Any

from core.claude_code.corpo import (
    CorpoIndisponivel,
    Lancamento,
    Leitura,
    PedidoDeRodada,
    Saude,
)

logger = logging.getLogger(__name__)

#: `claude.start` espera o Daniel no diálogo de confirmação do Mac.
_START_TIMEOUT_S = 180.0
_TIMEOUT_S = 20.0
#: Teto de bytes por leitura: o WS aguenta bem mais, mas o tick é a cada poucos
#: segundos e o stream de uma rodada raramente passa disto entre dois ticks.
_LEITURA_MAX = 256 * 1024


class CorpoLocal:
    nome = "local"

    def __init__(self, broadcaster: Any, device_id: str, *, modo_permissao: str = "auto") -> None:
        self._b = broadcaster
        self.device_id = device_id
        self.modo_permissao = modo_permissao if modo_permissao in ("auto", "acceptEdits") else "auto"

    async def _pedir(
        self, acao: str, args: dict[str, Any], timeout: float = _TIMEOUT_S, sem_resposta: str = ""
    ) -> dict[str, Any]:
        from core.notifications.device_control import DeviceOffline, DeviceTimeout

        try:
            resposta = await self._b.request(self.device_id, acao, args, timeout=timeout)
        except DeviceOffline as exc:
            raise CorpoIndisponivel("o desktop do Mac não está conectado", fora_do_ar=True) from exc
        except DeviceTimeout as exc:
            if sem_resposta:
                raise CorpoIndisponivel(sem_resposta) from exc
            raise CorpoIndisponivel(f"o Mac não respondeu a {acao}", fora_do_ar=True) from exc
        if not resposta.get("ok"):
            raise CorpoIndisponivel(str(resposta.get("error") or f"{acao} falhou no Mac"))
        return resposta.get("result") or {}

    async def saude(self, pasta: str = "") -> Saude:
        try:
            r = await self._pedir("claude.health", {"pasta": pasta} if pasta else {})
        except CorpoIndisponivel as exc:
            return Saude(ok=False, motivo=str(exc))
        conferida = r.get("pasta") if isinstance(r.get("pasta"), dict) else None
        logado = bool(r.get("logado"))
        metodo = str(r.get("metodo") or "")
        motivo = ""
        if not r.get("instalado"):
            motivo = "o Claude Code não está instalado no Mac"
        elif not logado:
            motivo = (
                "o Claude Code do Mac não está logado — abra o terminal, rode `claude` "
                "e faça `/login`; o Neo não faz login por você"
            )
        elif metodo and metodo != "claude.ai":
            # Uma ANTHROPIC_API_KEY no ambiente do app ganha do login, calada
            # (plano §3.2): a conta iria para a API em vez da assinatura.
            motivo = (
                f"o Claude Code do Mac está autenticado por `{metodo}`, não pela sua "
                "assinatura (tem uma ANTHROPIC_API_KEY no ambiente do app?)"
            )
        return Saude(
            ok=not motivo,
            motivo=motivo,
            versao=str(r.get("versao") or ""),
            logado=logado,
            metodo=metodo,
            plano=str(r.get("plano") or ""),
            pasta_ok=None if conferida is None else bool(conferida.get("ok")),
            pasta_motivo=str((conferida or {}).get("motivo") or ""),
        )

    async def lancar(self, pedido: PedidoDeRodada) -> Lancamento:
        r = await self._pedir(
            "claude.start",
            {
                "run_id": pedido.run_id,
                "cwd": pedido.cwd,
                "prompt": pedido.prompt,
                "flags": pedido.flags,
                "confirmar": pedido.confirmar,
            },
            timeout=_START_TIMEOUT_S,
            # O comando CHEGOU (offline é outro erro): o que segura o `claude.start`
            # é o diálogo de confirmação. "O Mac não respondeu" (job 40852ae7)
            # escondia isso.
            sem_resposta=(
                "ninguém permitiu no Mac em 3 minutos — o pedido de confirmação ficou sem "
                "resposta (pode ter aberto atrás de outra janela). Peça de novo com você na "
                "frente do Mac"
            ),
        )
        return Lancamento(run_id=pedido.run_id, cwd=str(r.get("cwd") or pedido.cwd), pid=r.get("pid"))

    async def ler(self, run_id: str, offset: int) -> Leitura:
        r = await self._pedir("claude.read", {"run_id": run_id, "offset": offset, "max_bytes": _LEITURA_MAX})
        return Leitura(
            pedaco=str(r.get("chunk") or ""),
            offset=int(r.get("offset") or offset),
            vivo=bool(r.get("vivo")),
            codigo_saida=r.get("exit_code"),
        )

    async def parar(self, run_id: str) -> None:
        await self._pedir("claude.stop", {"run_id": run_id})

    async def recolher(self, cwd: str, caminhos: list[str]) -> list[dict[str, Any]]:
        # Pela pasta do projeto, não pela da rodada (que some no fim de cada
        # rodada). O desktop recusa caminho que saia da pasta.
        r = await self._pedir("claude.collect", {"cwd": cwd, "paths": caminhos[:20]})
        return [a for a in r.get("files") or [] if isinstance(a, dict)]

    async def limpar(self, run_id: str) -> None:
        # Só a pasta da rodada. O histórico do Claude dele fica: `claude purge`
        # apaga o projeto INTEIRO (medido na F0), não uma sessão.
        try:
            await self._pedir("claude.cleanup", {"run_id": run_id})
        except CorpoIndisponivel as exc:
            logger.info("[claude-local] limpeza da rodada %s ficou para depois: %s", run_id, exc)

    def flags_de_permissao(self, liberadas: list[str]) -> list[str]:
        flags = ["--permission-mode", self.modo_permissao, "--permission-prompts", "none"]
        if liberadas:
            flags += ["--allowedTools", *liberadas]
        return flags
