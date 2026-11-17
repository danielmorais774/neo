"""Caixa de email do Neo — IMAP para ler, SMTP para enviar.

Nasceu de um pedido concreto: "cadastrar um email para o neo-core usar sempre
que precisar de um email — o email pessoal dele". Sem LER a caixa, isso funciona
pela metade: o Neo digita o endereço num cadastro e trava no código de
confirmação. Com esta camada ele busca o código sozinho e termina a tarefa.

**Envio (2026-08-26).** Antes só havia leitura, e o comentário aqui dizia que
enviar era "decisão de produto, não efeito colateral de uma tool de leitura".
A decisão foi tomada: o Neo envia — mas o escopo continua estreito de propósito.
Existe ler e enviar; **não existe apagar, mover nem responder-em-thread**, que
são as operações que estragam a caixa de forma difícil de desfazer. O envio é
sempre em nome do PRÓPRIO Neo (o ``From`` é o endereço da credencial), nunca em
nome do usuário, e fica atrás da flag ``NEO_EMAIL_SEND_ENABLED``.

``imaplib``/``smtplib`` são da stdlib e são BLOQUEANTES: toda chamada roda em
``asyncio.to_thread`` para não travar o loop do neo-core (é o mesmo processo que
atende o chat). O ``BODY.PEEK`` é de propósito — ler não marca a mensagem como
lida.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import mimetypes
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 20
MAX_FETCH = 25
SNIPPET_CHARS = 400
MAX_BODY_CHARS = 4000
#: Quantos anexos a listagem de UMA mensagem mostra (nome, tipo, tamanho).
MAX_ATTACHMENTS_LISTED = 20
MAX_RECIPIENTS = 5
MAX_SUBJECT_CHARS = 200
MAX_SEND_CHARS = 20000
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
# Validação folgada de propósito: o objetivo é barrar "manda pro fulano" virando
# destinatário, não reimplementar a RFC 5322 (que aceita coisas que nenhum
# servidor real entrega).
_ADDR = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


class MailboxError(Exception):
    """Falha de caixa (login, host, TLS) com mensagem já pronta para o humano."""


@dataclass
class MailSummary:
    uid: str
    subject: str
    sender: str
    date: str
    snippet: str
    unread: bool
    #: Nomes dos anexos. Sem isto um email que é SÓ uma foto chegava com prévia
    #: vazia e nada mais — e o Neo concluiu que "o anexo não veio" (19b47475).
    attachments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.uid,
            "de": self.sender,
            "assunto": self.subject,
            "quando": self.date,
            "previa": self.snippet,
            "nao_lida": self.unread,
        }
        if self.attachments:
            out["anexos"] = list(self.attachments)
        return out


def _decode(raw: Any) -> str:
    """Header MIME (``=?utf-8?B?…?=``) → texto legível. Nunca levanta."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(str(raw)))).strip()
    except Exception:  # noqa: BLE001
        return str(raw).strip()


def _body_text(msg: email.message.Message) -> str:
    """Texto da mensagem: prefere text/plain; cai para o HTML sem tags.

    Anexo é ignorado de propósito — o que interessa aqui é o texto (código de
    confirmação, link); um PDF em base64 no meio da resposta só queimaria contexto.
    """
    plains: list[str] = []
    htmls: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        (plains if ctype == "text/plain" else htmls).append(text)

    text = "\n".join(plains) or _HTML_TAG.sub(" ", "\n".join(htmls))
    text = _WS.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _attachment_parts(msg: email.message.Message) -> list[email.message.Message]:
    """As partes que são ARQUIVO (anexo ou imagem colada no corpo), em ordem.

    O critério não é só ``Content-Disposition: attachment``: o Gmail manda a foto
    arrastada para o corpo como ``inline`` dentro de um ``multipart/related``, às
    vezes sem disposição nenhuma, só com ``Content-ID``. O que separa arquivo de
    corpo é: tem nome, OU é ``attachment``, OU não é texto. Um ``text/plain``
    sem nome é o corpo da mensagem (é o que ``_body_text`` lê) e fica de fora.
    """
    parts: list[email.message.Message] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        # O email encaminhado (message/rfc822) é desdobrado pelo walk; o
        # contêiner em si não é arquivo.
        if ctype == "message/rfc822":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        has_name = bool(part.get_filename())
        if part.get_content_maintype() == "text" and not has_name and disposition != "attachment":
            continue
        parts.append(part)
    return parts


def _attachment_name(part: email.message.Message, index: int) -> str:
    """Nome legível do anexo; sem nome, um nome pelo tipo (``anexo-2.jpg``)."""
    name = _decode(part.get_filename())
    if name:
        # Nome vem de fora: só o último componente, nunca um caminho.
        return name.replace("\\", "/").rsplit("/", 1)[-1].strip() or f"anexo-{index}"
    ext = mimetypes.guess_extension(part.get_content_type()) or ""
    if ext == ".jpe":
        ext = ".jpg"
    return f"anexo-{index}{ext}"


def _attachments_summary(msg: email.message.Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, part in enumerate(_attachment_parts(msg)[:MAX_ATTACHMENTS_LISTED], start=1):
        try:
            size = len(part.get_payload(decode=True) or b"")
        except Exception:  # noqa: BLE001
            size = None
        out.append({
            "indice": i,
            "nome": _attachment_name(part, i),
            "tipo": part.get_content_type(),
            "tamanho_bytes": size,
        })
    return out


def _local_time(when: datetime) -> str:
    """``dd/mm HH:MM`` no fuso do Neo (o do usuário), não no do container.

    O container roda em UTC: o email que o Daniel mandou às 23:23 de 14/11
    aparecia como "15/11 02:23" (19b47475). O fuso padrão é o da agenda, e o
    import é tardio porque `core.scheduler` puxa o pacote inteiro.
    """
    try:
        from core.scheduler.timing import DEFAULT_TZ, get_tz

        return when.astimezone(get_tz(DEFAULT_TZ)).strftime("%d/%m %H:%M")
    except Exception:  # noqa: BLE001 — formatar hora nunca derruba a leitura
        return when.astimezone(timezone.utc).strftime("%d/%m %H:%M UTC")


def _connect(*, host: str, port: int, address: str, password: str) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=CONNECT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        raise MailboxError(f"não consegui falar com {host}:{port} — {exc}") from exc
    try:
        conn.login(address, password)
    except imaplib.IMAP4.error as exc:
        # O erro cru do Gmail ("Invalid credentials (Failure)") não diz a causa
        # real, que quase sempre é senha normal no lugar da senha de app.
        raise MailboxError(
            f"login recusado para {address}. Se for Gmail, o segredo tem que ser uma "
            "SENHA DE APP (Conta Google → Segurança → Senhas de app), não a senha "
            f"da conta, e o IMAP precisa estar habilitado. Detalhe: {exc}"
        ) from exc
    return conn


def _search_recent(conn: imaplib.IMAP4_SSL, since: datetime, unread_only: bool) -> list[bytes]:
    """UIDs das mensagens a partir de ``since``.

    O SEARCH do IMAP tem granularidade de DIA (``SINCE 25-Aug-2026``), então o
    corte fino por minutos é feito depois, no header Date de cada mensagem.

    **O dia pedido ao servidor é um ANTES do de ``since``.** O ``SINCE`` compara
    com a data que o SERVIDOR dá à mensagem, no fuso DELE, e ``since`` está em UTC.
    Sessão 19b47475 (14/11, 23:24 BRT = 02:24 UTC de 15/11): a busca de 30 min
    virou ``SINCE 15-Nov`` e o Gmail, que datou a mensagem das 23:23 em 14/11,
    devolveu zero — duas vezes, com o email lá. As janelas de 24 h e 7 dias
    acharam a mesma mensagem. Toda noite, da meia-noite UTC (21h em Brasília) até
    a virada do dia no fuso do servidor, uma janela curta ficava cega. Um dia a
    mais custa no máximo ``MAX_FETCH`` mensagens a mais lidas, e o corte pelo
    header descarta todas.
    """
    dia = since - timedelta(days=1)
    criteria = [f'SINCE "{dia.strftime("%d-%b-%Y")}"']
    if unread_only:
        criteria.append("UNSEEN")
    typ, data = conn.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
    if typ != "OK":
        raise MailboxError(f"busca IMAP falhou: {typ}")
    return (data[0] or b"").split()


def _fetch_summaries(
    conn: imaplib.IMAP4_SSL, uids: list[bytes], *, since: datetime, limit: int
) -> list[MailSummary]:
    out: list[MailSummary] = []
    for uid in reversed(uids[-MAX_FETCH:]):  # mais novas primeiro
        if len(out) >= limit:
            break
        typ, data = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        flags_blob = (data[0][0] or b"").decode("utf-8", errors="replace")
        msg = email.message_from_bytes(data[0][1])
        when = None
        try:
            when = parsedate_to_datetime(msg.get("Date"))
        except Exception:  # noqa: BLE001
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < since:
                continue
        body = _body_text(msg)
        out.append(
            MailSummary(
                uid=uid.decode(),
                subject=_decode(msg.get("Subject")) or "(sem assunto)",
                sender=_decode(msg.get("From")),
                date=_local_time(when) if when else "",
                snippet=body[:SNIPPET_CHARS],
                unread="\\Seen" not in flags_blob,
                attachments=tuple(a["nome"] for a in _attachments_summary(msg)),
            )
        )
    return out


def _logout(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        pass


def _inbox_sync(
    *, host: str, port: int, address: str, password: str,
    minutes: int, limit: int, unread_only: bool, folder: str,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    conn = _connect(host=host, port=port, address=address, password=password)
    try:
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            raise MailboxError(f"não consegui abrir a pasta '{folder}'.")
        uids = _search_recent(conn, since, unread_only)
        return [m.to_dict() for m in _fetch_summaries(conn, uids, since=since, limit=limit)]
    finally:
        _logout(conn)


def _message_sync(
    *, host: str, port: int, address: str, password: str, uid: str, folder: str
) -> dict[str, Any] | None:
    conn = _connect(host=host, port=port, address=address, password=password)
    try:
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            raise MailboxError(f"não consegui abrir a pasta '{folder}'.")
        typ, data = conn.uid("FETCH", uid.encode(), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        msg = email.message_from_bytes(data[0][1])
        body = _body_text(msg)
        return {
            "id": uid,
            "de": _decode(msg.get("From")),
            "para": _decode(msg.get("To")),
            "assunto": _decode(msg.get("Subject")) or "(sem assunto)",
            "quando": _decode(msg.get("Date")),
            "texto": body[:MAX_BODY_CHARS],
            "truncado": len(body) > MAX_BODY_CHARS,
            "anexos": _attachments_summary(msg),
        }
    finally:
        _logout(conn)


def _attachment_sync(
    *, host: str, port: int, address: str, password: str, uid: str, index: int, folder: str
) -> dict[str, Any] | None:
    """O anexo ``index`` (1 = primeiro, a mesma ordem de ``anexos``) em bytes.

    ``None`` quando a mensagem não existe; ``{"anexos": [...]}`` sem ``dados``
    quando o índice não casa — quem chama mostra os que existem.
    """
    conn = _connect(host=host, port=port, address=address, password=password)
    try:
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            raise MailboxError(f"não consegui abrir a pasta '{folder}'.")
        typ, data = conn.uid("FETCH", uid.encode(), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        msg = email.message_from_bytes(data[0][1])
        parts = _attachment_parts(msg)
        base = {
            "de": _decode(msg.get("From")),
            "assunto": _decode(msg.get("Subject")) or "(sem assunto)",
            "anexos": _attachments_summary(msg),
        }
        if not 1 <= index <= len(parts):
            return base
        part = parts[index - 1]
        return {
            **base,
            "nome": _attachment_name(part, index),
            "tipo": part.get_content_type(),
            "dados": part.get_payload(decode=True) or b"",
        }
    finally:
        _logout(conn)


async def list_inbox(
    *, host: str, port: int, address: str, password: str,
    minutes: int = 60, limit: int = 10, unread_only: bool = False, folder: str = "INBOX",
) -> list[dict[str, Any]]:
    """Mensagens recentes (mais novas primeiro), sem marcar nada como lido."""
    return await asyncio.to_thread(
        _inbox_sync, host=host, port=port, address=address, password=password,
        minutes=minutes, limit=limit, unread_only=unread_only, folder=folder,
    )


async def get_message(
    *, host: str, port: int, address: str, password: str, uid: str, folder: str = "INBOX"
) -> dict[str, Any] | None:
    """Uma mensagem inteira (texto + lista de anexos), pelo id de :func:`list_inbox`."""
    return await asyncio.to_thread(
        _message_sync, host=host, port=port, address=address,
        password=password, uid=uid, folder=folder,
    )


async def get_attachment(
    *, host: str, port: int, address: str, password: str, uid: str, index: int,
    folder: str = "INBOX",
) -> dict[str, Any] | None:
    """Os bytes de UM anexo — ver :func:`_attachment_sync`."""
    return await asyncio.to_thread(
        _attachment_sync, host=host, port=port, address=address,
        password=password, uid=uid, index=index, folder=folder,
    )


# ----------------------------------------------------------------------
# Envio (SMTP)
# ----------------------------------------------------------------------


def smtp_from_imap(imap_host: str) -> str:
    """Deriva o servidor SMTP do IMAP quando ninguém cadastrou o SMTP.

    Praticamente todo provedor usa o mesmo domínio trocando o prefixo
    (``imap.gmail.com`` → ``smtp.gmail.com``). Derivar evita obrigar o usuário a
    reabrir o cofre e preencher um campo que ele já preencheu de outro jeito —
    e o campo ``smtp_host`` continua existindo para quem foge do padrão.
    """
    host = (imap_host or "").strip().lower()
    if host.startswith("imap."):
        return "smtp." + host[len("imap.") :]
    if host.startswith("mail."):
        return host
    return f"smtp.{host}" if host and not host.startswith("smtp.") else host


def normalize_recipients(raw: Any) -> list[str]:
    """Aceita string ("a@x, b@y") ou lista e devolve endereços validados.

    Levanta ``MailboxError`` com o endereço problemático em vez de silenciosamente
    descartar: um destinatário perdido no meio de uma lista é o tipo de falha que
    ninguém percebe até o email não chegar.
    """
    if isinstance(raw, str):
        partes = [p for p in re.split(r"[,;]", raw)]
    elif isinstance(raw, (list, tuple)):
        partes = [str(p) for p in raw]
    else:
        partes = []
    out: list[str] = []
    for parte in partes:
        addr = parte.strip().strip("<>").strip()
        if not addr:
            continue
        if not _ADDR.match(addr):
            raise MailboxError(f"destinatário inválido: {addr!r}")
        if addr not in out:
            out.append(addr)
    if not out:
        raise MailboxError("faltou o destinatário ('para').")
    if len(out) > MAX_RECIPIENTS:
        raise MailboxError(
            f"máximo de {MAX_RECIPIENTS} destinatários por envio (vieram {len(out)})."
        )
    return out


def _smtp_connect(*, host: str, port: int, address: str, password: str) -> smtplib.SMTP:
    """Conecta e autentica. 465 = SSL direto; qualquer outra porta = STARTTLS.

    Sem TLS não há envio: a senha de app iria em claro pela rede.
    """
    try:
        if port == 465:
            conn: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=CONNECT_TIMEOUT_S)
        else:
            conn = smtplib.SMTP(host, port, timeout=CONNECT_TIMEOUT_S)
            conn.ehlo()
            conn.starttls()
            conn.ehlo()
    except Exception as exc:  # noqa: BLE001
        raise MailboxError(f"não consegui falar com {host}:{port} — {exc}") from exc
    try:
        conn.login(address, password)
    except smtplib.SMTPAuthenticationError as exc:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001
            pass
        # Mesma armadilha do IMAP: senha da conta no lugar da senha de app.
        raise MailboxError(
            f"login SMTP recusado para {address}. Se for Gmail, o segredo tem que ser "
            "uma SENHA DE APP (Conta Google → Segurança → Senhas de app), não a senha "
            f"da conta. Detalhe: {exc}"
        ) from exc
    return conn


def _send_sync(
    *, host: str, port: int, address: str, password: str,
    to: list[str], subject: str, body: str, display_name: str | None,
) -> dict[str, Any]:
    msg = EmailMessage()
    msg["From"] = formataddr((display_name or "Neo", address))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    conn = _smtp_connect(host=host, port=port, address=address, password=password)
    try:
        recusados = conn.send_message(msg)
    except smtplib.SMTPException as exc:
        raise MailboxError(f"o servidor recusou o envio: {exc}") from exc
    finally:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001
            pass

    entregues = [a for a in to if a not in (recusados or {})]
    # Log de auditoria: envio é a única operação DE SAÍDA do Neo. Assunto e
    # destinatário no log, corpo nunca.
    logger.info(
        "[email] enviado de=%s para=%s assunto=%r recusados=%s",
        address, ",".join(entregues), subject[:80], sorted(recusados or {}),
    )
    return {
        "enviado": bool(entregues),
        "para": entregues,
        "recusados": sorted(recusados or {}),
        "assunto": subject,
        "message_id": msg["Message-ID"],
    }


async def send_message(
    *, host: str, port: int, address: str, password: str,
    to: list[str], subject: str, body: str, display_name: str | None = None,
) -> dict[str, Any]:
    """Envia um email de texto simples EM NOME DO NEO (``From`` = ``address``)."""
    return await asyncio.to_thread(
        _send_sync, host=host, port=port, address=address, password=password,
        to=to, subject=subject, body=body, display_name=display_name,
    )
