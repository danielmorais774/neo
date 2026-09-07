"""Cofre → browser: monta o ``sensitive_data`` do subagente (F3 do browser-agent).

O contrato da browser-use: ``{padrão_de_domínio: {placeholder: valor}}``. O LLM
do subagente vê **só os placeholders**, e só quando a aba está NUM domínio que
casa com o padrão — ele escreve ``<secret>github_pessoal_pass</secret>`` e a lib
troca pelo valor na hora de digitar. Segredo nenhum entra no prompt dele, e uma
página de outro domínio não consegue induzi-lo a revelar (o placeholder sequer é
mencionado fora do escopo).

Regras deste módulo — a política de "que credencial pode ir para o browser":

- ``password``/``basic`` **exigem domínio**. Sem domínio o valor viraria global e
  qualquer página conseguiria colher a senha via prompt injection; em vez de
  degradar em silêncio, recusamos com uma mensagem que diz como resolver.
- ``email`` entra **só com o endereço, em claro**: endereço é público (é o que se
  digita num cadastro) e a senha da caixa não tem o que fazer num formulário
  qualquer. Para ler a caixa existe o IMAP (``email_inbox``), que não passa pelo
  browser.
- ``token``/``api_key``/``cookie``/``cert`` não vão: são para o gateway HTTP, não
  para preencher formulário.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_SLUG = re.compile(r"[^a-z0-9]+")
MAX_CREDENTIALS = 4


@dataclass
class BrowserCredentials:
    """O que o runner precisa para chamar o wrapper com credenciais."""

    sensitive_data: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Linhas para o CONTEXTO do subagente (sem segredo) — dizem o que existe.
    notes: list[str] = field(default_factory=list)
    #: O que não pôde ser usado, em português, para voltar ao usuário.
    problems: list[str] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return bool(self.sensitive_data or self.notes)


def _slug(label: str) -> str:
    return _SLUG.sub("_", (label or "").lower()).strip("_")[:40] or "cred"


def normalize_domain(raw: str) -> str:
    """'github.com' → '*.github.com' (casa o apex E os subdomínios, só https).

    Padrão com esquema ou glob explícito passa intacto: quem escreveu
    'http*://interno.local' sabe o que quer.
    """
    value = (raw or "").strip().lower()
    value = re.sub(r"^[a-z*]+://", lambda m: m.group(0), value)  # preserva esquema
    if "://" in value or "*" in value:
        return value
    value = value.split("/")[0].strip()
    return f"*.{value}" if value else value


async def build_browser_credentials(store: Any, labels: list[str]) -> BrowserCredentials:
    """Resolve os rótulos e devolve o material pronto (ou o motivo de não dar).

    Nunca levanta: o job de browser continua valendo sem credencial — só sem
    login. O que não deu vira ``problems`` e chega ao usuário no fim.
    """
    out = BrowserCredentials()
    if store is None:
        out.problems.append(
            "o cofre está indisponível (NEO_CRED_KEY não configurada), então não deu para "
            "usar as credenciais pedidas."
        )
        return out

    for label in labels[:MAX_CREDENTIALS]:
        label = str(label or "").strip()
        if not label:
            continue
        try:
            resolved = await store.resolve(label)
        except Exception as exc:  # noqa: BLE001 — chave trocada, etc.
            out.problems.append(f"não consegui abrir a credencial '{label}': {exc}")
            continue
        if resolved is None:
            out.problems.append(f"não existe credencial '{label}' no cofre.")
            continue
        info, secret = resolved
        fields = info.fields or {}
        slug = _slug(info.label)

        if info.kind == "email":
            address = str(fields.get("address") or "").strip()
            if not address:
                out.problems.append(f"a credencial '{label}' não tem endereço cadastrado.")
                continue
            # Endereço é público: vai em claro, que é como se digita num cadastro.
            out.notes.append(
                f"Email para usar quando o site pedir um endereço: {address}. "
                "Se chegar código/link de confirmação, o orquestrador lê a caixa e continua."
            )
            continue

        if info.kind in ("password", "basic"):
            username = str(fields.get("username") or "").strip()
            domain = normalize_domain(str(fields.get("domain") or ""))
            if not domain:
                out.problems.append(
                    f"a credencial '{label}' não tem 'Domínio de uso' preenchido — sem isso a "
                    "senha ficaria disponível em qualquer página, e eu não faço isso. "
                    "Preencha o domínio no neo-panel (Serviços → Cofre)."
                )
                continue
            entry = out.sensitive_data.setdefault(domain, {})
            user_key, pass_key = f"{slug}_user", f"{slug}_pass"
            if username:
                entry[user_key] = username
            entry[pass_key] = secret
            campos = f"<secret>{user_key}</secret> e " if username else ""
            out.notes.append(
                f"Login de {domain} ({info.description or info.label}): use {campos}"
                f"<secret>{pass_key}</secret> nos campos do formulário — os valores reais "
                "são injetados pelo sistema; NUNCA tente adivinhar ou reescrever."
            )
            continue

        out.problems.append(
            f"a credencial '{label}' é do tipo '{info.kind}', que não serve para preencher "
            "formulário no browser (esse tipo é para chamadas HTTP do gateway)."
        )

    if len(labels) > MAX_CREDENTIALS:
        out.problems.append(
            f"usei só as {MAX_CREDENTIALS} primeiras credenciais — mais que isso numa tarefa "
            "só costuma ser engano."
        )
    return out
