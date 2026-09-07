"""Tools de email do Neo — ler a caixa DELE (nunca a do usuário).

A caixa é uma credencial ``kind="email"`` no cofre: endereço + host/porta IMAP em
claro, senha de app cifrada. O cérebro nunca vê a senha; ele cita o rótulo (ou
nem isso, quando só existe uma caixa) e o executor resolve.

Por que o endereço vive no cofre e não numa env: é assim que o painel cadastra,
e é assim que ``vault_list`` deixa o cérebro DESCOBRIR que existe um email
disponível — sem redeploy e sem que ninguém edite .env para trocar de caixa.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.mailbox.client import (
    MAX_SEND_CHARS,
    MAX_SUBJECT_CHARS,
    MailboxError,
    get_message,
    list_inbox,
    normalize_recipients,
    send_message,
    smtp_from_imap,
)
from shared import ToolCall, ToolParameter, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

EMAIL_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="email_inbox",
        description=(
            "Lê as mensagens RECENTES da caixa de email do próprio Neo (a que está "
            "cadastrada no cofre). Use para pegar código/link de confirmação de um "
            "cadastro que VOCÊ fez, conferir se uma mensagem chegou, ou responder "
            "'chegou algum email?'. Devolve remetente, assunto, quando e uma prévia "
            "do texto — para o texto completo use email_message. NÃO marca nada como "
            "lido. Esta é a caixa do NEO, não a do usuário."
        ),
        module="email",
        execution="server-side",
        timeout=45,
        parameters={
            "minutes": ToolParameter(
                type="integer",
                description=(
                    "Janela em minutos (opcional; padrão 60). Para um código que "
                    "acabou de ser pedido, 5-10 já basta."
                ),
                required=False,
            ),
            "limit": ToolParameter(
                type="integer",
                description="Quantas mensagens no máximo (opcional; padrão 10, teto 25).",
                required=False,
            ),
            "unread_only": ToolParameter(
                type="boolean",
                description="Só as não lidas (opcional).",
                required=False,
            ),
            "credential": ToolParameter(
                type="string",
                description=(
                    "Rótulo da caixa no cofre (opcional). Só é necessário se houver "
                    "mais de uma cadastrada — use vault_list para ver."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="email_message",
        description=(
            "Abre UMA mensagem da caixa do Neo e devolve o texto completo. Use "
            "depois do email_inbox, com o 'id' que ele deu, quando a prévia não "
            "bastar (link de confirmação no meio do texto, instruções longas)."
        ),
        module="email",
        execution="server-side",
        timeout=45,
        parameters={
            "id": ToolParameter(
                type="string",
                description="O 'id' da mensagem, exatamente como veio no email_inbox.",
                required=True,
            ),
            "credential": ToolParameter(
                type="string",
                description="Rótulo da caixa no cofre (opcional; só com mais de uma).",
                required=False,
            ),
        },
    ),
]

#: Envio fica numa lista SEPARADA porque é gated por `NEO_EMAIL_SEND_ENABLED`:
#: ler é reversível, enviar não. Quem monta o toolset (api/main.py e
#: core/cognitive/read_tools.py) decide se soma esta lista à de leitura.
EMAIL_SEND_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="email_send",
        description=(
            "Envia um email DA CAIXA DO PRÓPRIO NEO (o remetente é sempre ele, nunca "
            "o usuário). Use quando pedirem para você mandar/enviar/escrever um email "
            "para alguém. Texto simples, sem anexo. O email SAI de verdade e não tem "
            "como cancelar depois: confira destinatário e assunto antes de chamar, e "
            "se o usuário não disse para quem, PERGUNTE em vez de adivinhar."
        ),
        module="email",
        execution="server-side",
        timeout=45,
        parameters={
            "para": ToolParameter(
                type="string",
                description=(
                    "Destinatário. Vários, separados por vírgula (máximo 5). Precisa "
                    "ser um endereço completo — nome de pessoa não serve."
                ),
                required=True,
            ),
            "assunto": ToolParameter(
                type="string",
                description="Assunto da mensagem. Curto e específico.",
                required=True,
            ),
            "corpo": ToolParameter(
                type="string",
                description=(
                    "Texto do email. Escreva como email de verdade (saudação, corpo, "
                    "assinatura), não como resposta de chat. Sem markdown — vai como "
                    "texto puro."
                ),
                required=True,
            ),
            "credential": ToolParameter(
                type="string",
                description=(
                    "Rótulo da caixa no cofre (opcional). Só é necessário se houver "
                    "mais de uma cadastrada."
                ),
                required=False,
            ),
        },
    ),
]

EMAIL_TOOL_NAMES = frozenset(t.name for t in EMAIL_TOOLS)
EMAIL_SEND_TOOL_NAMES = frozenset(t.name for t in EMAIL_SEND_TOOLS)


class MailboxToolExecutor:
    """Executa as tools de email resolvendo a caixa no cofre."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        args = tool_call.arguments or {}
        try:
            if tool_call.name == "email_inbox":
                result = await self._inbox(args)
            elif tool_call.name == "email_message":
                result = await self._message(args)
            elif tool_call.name == "email_send":
                result = await self._send(args)
            else:
                result = {"ok": False, "error": f"tool desconhecida: {tool_call.name}"}
        except MailboxError as exc:
            result = {"ok": False, "error": str(exc)[:400]}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[email] %s falhou", tool_call.name)
            result = {"ok": False, "error": f"falha ao acessar a caixa: {exc}"[:300]}
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            result=result,
            error=None if result.get("ok") else str(result.get("error") or "")[:300],
            execution_time=asyncio.get_event_loop().time() - start,
        )

    # ------------------------------------------------------------------

    async def _resolve_box(self, label: str | None) -> tuple[Any, str]:
        """Acha a credencial da caixa no cofre: ``(info, segredo)``.

        Sem rótulo e com uma única caixa cadastrada, usa aquela — é o caso normal
        ("o email do Neo"). Com várias, exige escolha em vez de adivinhar.
        """
        if self._store is None:
            raise MailboxError(
                "o cofre está indisponível (NEO_CRED_KEY não configurada) — sem ele não "
                "há como guardar a senha da caixa."
            )
        if label:
            resolved = await self._store.resolve(str(label))
            if resolved is None:
                raise MailboxError(f"não existe credencial '{label}' no cofre.")
            info, secret = resolved
            if info.kind != "email":
                raise MailboxError(
                    f"a credencial '{label}' é do tipo '{info.kind}', não é uma caixa de email."
                )
        else:
            boxes = await self._store.list_by_kind("email")
            if not boxes:
                raise MailboxError(
                    "nenhuma caixa de email cadastrada no cofre. O usuário precisa cadastrá-la "
                    "no neo-panel (Serviços → Cofre → tipo 'Caixa de email'): endereço, servidor "
                    "IMAP e uma senha de app."
                )
            if len(boxes) > 1:
                nomes = ", ".join(b.label for b in boxes)
                raise MailboxError(
                    f"há mais de uma caixa cadastrada ({nomes}) — diga qual usar no parâmetro "
                    "'credential'."
                )
            resolved = await self._store.resolve(boxes[0].label)
            if resolved is None:
                raise MailboxError("a caixa sumiu do cofre no meio da consulta.")
            info, secret = resolved
        return info, secret

    async def _mailbox(self, label: str | None) -> tuple[dict[str, Any], str]:
        """Coordenadas de LEITURA (IMAP) da caixa + senha de app."""
        info, secret = await self._resolve_box(label)
        fields = info.fields or {}
        address = str(fields.get("address") or "").strip()
        host = str(fields.get("imap_host") or "").strip()
        if not address or not host:
            raise MailboxError(
                f"a credencial '{info.label}' está incompleta: falta endereço e/ou servidor IMAP. "
                "Complete no neo-panel (Serviços → Cofre)."
            )
        try:
            port = int(fields.get("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        await self._store.touch(info.id)
        return {"host": host, "port": port, "address": address, "password": secret}, info.label

    async def _smtp(self, label: str | None) -> tuple[dict[str, Any], str]:
        """Coordenadas de ENVIO (SMTP) da mesma caixa.

        O SMTP é derivado do IMAP quando não foi cadastrado — ver
        ``smtp_from_imap``. Por isso enviar não exige que ninguém volte ao painel
        para completar a credencial que já existe.
        """
        info, secret = await self._resolve_box(label)
        fields = info.fields or {}
        address = str(fields.get("address") or "").strip()
        host = str(fields.get("smtp_host") or "").strip() or smtp_from_imap(
            str(fields.get("imap_host") or "")
        )
        if not address or not host:
            raise MailboxError(
                f"a credencial '{info.label}' está incompleta: falta endereço e/ou servidor "
                "(SMTP ou IMAP). Complete no neo-panel (Serviços → Cofre)."
            )
        try:
            port = int(fields.get("smtp_port") or 587)
        except (TypeError, ValueError):
            port = 587
        await self._store.touch(info.id)
        return {"host": host, "port": port, "address": address, "password": secret}, info.label

    async def _inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        conn, label = await self._mailbox(args.get("credential"))
        minutes = _int(args.get("minutes"), default=60, low=1, high=60 * 24 * 14)
        limit = _int(args.get("limit"), default=10, low=1, high=25)
        messages = await list_inbox(
            **conn,
            minutes=minutes,
            limit=limit,
            unread_only=bool(args.get("unread_only")),
        )
        return {
            "ok": True,
            "caixa": conn["address"],
            "credencial": label,
            "janela_min": minutes,
            "count": len(messages),
            "mensagens": messages,
            "note": (
                "Nada aqui? A mensagem pode não ter chegado ainda — espere alguns segundos "
                "e consulte de novo antes de concluir que falhou."
                if not messages
                else "Use email_message com o 'id' para ler uma inteira."
            ),
        }

    async def _message(self, args: dict[str, Any]) -> dict[str, Any]:
        uid = str(args.get("id") or "").strip()
        if not uid:
            return {"ok": False, "error": "faltou o 'id' da mensagem (veio do email_inbox)."}
        conn, label = await self._mailbox(args.get("credential"))
        msg = await get_message(**conn, uid=uid)
        if msg is None:
            return {"ok": False, "error": f"mensagem '{uid}' não encontrada na caixa."}
        return {"ok": True, "caixa": conn["address"], "credencial": label, "mensagem": msg}

    async def _send(self, args: dict[str, Any]) -> dict[str, Any]:
        """Envia. Valida ANTES de conectar — erro de argumento não custa um SMTP."""
        to = normalize_recipients(args.get("para") or args.get("to"))
        subject = str(args.get("assunto") or args.get("subject") or "").strip()
        body = str(args.get("corpo") or args.get("body") or "").strip()
        if not subject:
            return {"ok": False, "error": "faltou o assunto."}
        if not body:
            return {"ok": False, "error": "faltou o corpo do email."}
        if len(subject) > MAX_SUBJECT_CHARS:
            return {
                "ok": False,
                "error": f"assunto passa de {MAX_SUBJECT_CHARS} caracteres.",
            }
        if len(body) > MAX_SEND_CHARS:
            return {
                "ok": False,
                "error": f"corpo passa de {MAX_SEND_CHARS} caracteres.",
            }
        conn, label = await self._smtp(args.get("credential"))
        info = await send_message(**conn, to=to, subject=subject, body=body)
        if not info.get("enviado"):
            return {
                "ok": False,
                "error": f"nenhum destinatário aceito: {', '.join(info.get('recusados') or [])}",
            }
        return {
            "ok": True,
            "caixa": conn["address"],
            "credencial": label,
            **info,
            "note": "O email SAIU. Não existe desfazer — não reenvie 'por garantia'.",
        }


def _int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default
