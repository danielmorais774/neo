"""O que cada TIPO de credencial guarda ALÉM do segredo.

Antes desta spec o ``kind`` era decorativo: qualquer tipo abria o mesmo
formulário de um campo só ("Segredo"), então **usuário+senha não tinha onde
morar** — o username acabava do outro lado, no ``auth_config`` da conexão do
gateway, e um login de site sem conexão nenhuma simplesmente não cabia no cofre.
Aqui o tipo passa a definir os campos.

Dois destinos, e a diferença entre eles é a regra de ouro deste módulo:

- **Segredo** (um por credencial): cifrado com Fernet, write-only, nunca sai em
  claro para o painel nem para nenhum LLM.
- **Campos** (``fields``): identificação NÃO secreta — usuário, endereço de
  email, host, porta, domínio de uso. Ficam **em claro** no banco de propósito:
  é o que deixa o painel mostrar "usuário: daniel" sem decifrar nada e o cérebro
  escolher a credencial certa sem nunca tocar no segredo.

Como os nomes de campo vêm desta spec (whitelist), não existe "campo livre"
onde um segundo segredo pudesse ser digitado sem querer — o store recusa
qualquer chave que não esteja aqui.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldSpec:
    """Um campo não-secreto de uma credencial (o painel renderiza a partir daqui)."""

    name: str
    label: str
    required: bool = False
    placeholder: str = ""
    type: str = "text"  # text | number
    help: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "required": self.required,
            "placeholder": self.placeholder,
            "type": self.type,
            "help": self.help,
        }


@dataclass(frozen=True)
class KindSpec:
    """Um tipo de credencial: como se chama o segredo dele e que campos o acompanham."""

    kind: str
    label: str
    secret_label: str
    secret_placeholder: str = ""
    secret_help: str = ""
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    description_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "secret_label": self.secret_label,
            "secret_placeholder": self.secret_placeholder,
            "secret_help": self.secret_help,
            "fields": [f.to_dict() for f in self.fields],
            "description_hint": self.description_hint,
        }


# O domínio é o que amarra a credencial ao lugar onde ela vale. Vale dobrado no
# browser: é ele que faz o `sensitive_data` ser escopado por site, para o
# subagente não conseguir vazar a senha do banco num formulário qualquer.
_DOMAIN = FieldSpec(
    name="domain",
    label="Domínio de uso",
    placeholder="github.com",
    help="Onde esta credencial vale. O browser só a usa em páginas deste domínio.",
)

CREDENTIAL_KIND_SPECS: tuple[KindSpec, ...] = (
    KindSpec(
        kind="password",
        label="Usuário e senha",
        secret_label="Senha",
        secret_placeholder="••••••••",
        secret_help="Só a senha — o usuário vai no campo ao lado, em claro.",
        fields=(
            FieldSpec(
                name="username",
                label="Usuário",
                required=True,
                placeholder="daniel@exemplo.com",
                help="Login, email ou apelido usado para entrar.",
            ),
            _DOMAIN,
        ),
        description_hint="Para que serve e quando usar (ex.: 'login do meu GitHub pessoal').",
    ),
    KindSpec(
        kind="email",
        label="Caixa de email",
        secret_label="Senha de app",
        secret_placeholder="xxxx xxxx xxxx xxxx",
        secret_help=(
            "No Gmail, use uma SENHA DE APP (Conta Google → Segurança → Senhas de app), "
            "nunca a senha principal. Exige 2FA ligado na conta."
        ),
        fields=(
            FieldSpec(
                name="address",
                label="Endereço",
                required=True,
                placeholder="neo@gmail.com",
                help="O endereço em si. É este que o Neo informa quando um site pede um email.",
            ),
            FieldSpec(
                name="imap_host",
                label="Servidor IMAP",
                required=True,
                placeholder="imap.gmail.com",
                help="Para o Neo LER a caixa (confirmações, códigos).",
            ),
            FieldSpec(
                name="imap_port",
                label="Porta IMAP",
                type="number",
                placeholder="993",
                help="Padrão 993 (SSL).",
            ),
            # SMTP é OPCIONAL: quando falta, é derivado do IMAP trocando o
            # prefixo (imap.gmail.com → smtp.gmail.com), que cobre praticamente
            # todo provedor. Os campos existem para quem foge do padrão — e para
            # não obrigar quem já cadastrou a caixa a voltar aqui só por causa
            # do envio.
            FieldSpec(
                name="smtp_host",
                label="Servidor SMTP",
                placeholder="smtp.gmail.com",
                help="Para o Neo ENVIAR. Vazio → derivado do servidor IMAP.",
            ),
            FieldSpec(
                name="smtp_port",
                label="Porta SMTP",
                type="number",
                placeholder="587",
                help="Padrão 587 (STARTTLS). Use 465 para SSL direto.",
            ),
        ),
        description_hint="Ex.: 'email pessoal do Neo — usar em cadastros que ele fizer sozinho'.",
    ),
    KindSpec(
        kind="token",
        label="Token",
        secret_label="Token",
        secret_placeholder="ghp_…",
        fields=(_DOMAIN,),
        description_hint="Que serviço/escopo este token abre.",
    ),
    KindSpec(
        kind="api_key",
        label="Chave de API",
        secret_label="Chave",
        secret_placeholder="sk-…",
        fields=(_DOMAIN,),
        description_hint="Que API esta chave abre e com que limites.",
    ),
    KindSpec(
        kind="basic",
        label="HTTP Basic",
        secret_label="Senha",
        fields=(
            FieldSpec(name="username", label="Usuário", required=True),
            _DOMAIN,
        ),
        description_hint="Que endpoint usa este Basic.",
    ),
    KindSpec(
        kind="cookie",
        label="Cookie de sessão",
        secret_label="Cookie",
        fields=(_DOMAIN,),
        description_hint="De que site é a sessão e quando costuma expirar.",
    ),
    KindSpec(
        kind="cert",
        label="Certificado / chave privada",
        secret_label="Conteúdo (PEM)",
        fields=(_DOMAIN,),
    ),
    KindSpec(
        kind="other",
        label="Outro",
        secret_label="Segredo",
        fields=(_DOMAIN,),
    ),
)

SPEC_BY_KIND: dict[str, KindSpec] = {s.kind: s for s in CREDENTIAL_KIND_SPECS}
# A ordem da tupla é a ordem do <select> no painel: os dois tipos que motivaram
# esta spec (login de site e caixa de email) vêm primeiro de propósito.
CREDENTIAL_KINDS: tuple[str, ...] = tuple(SPEC_BY_KIND)

MAX_FIELD_LEN = 200


class FieldError(ValueError):
    """Campo inválido para o tipo — mensagem já pronta para o humano."""


def normalize_fields(kind: str, fields: dict | None) -> dict:
    """Valida os campos contra a spec do tipo e devolve o dict limpo.

    Recusa chave desconhecida (é a whitelist que impede um segredo de entrar em
    claro num "campo livre"), exige os obrigatórios e converte porta para int.
    """
    spec = SPEC_BY_KIND.get(kind)
    if spec is None:
        raise FieldError(f"tipo inválido: {kind!r}")
    raw = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
    known = {f.name: f for f in spec.fields}
    unknown = set(raw) - set(known)
    if unknown:
        raise FieldError(
            f"campo(s) não previsto(s) para o tipo '{kind}': {', '.join(sorted(unknown))}. "
            f"Aceitos: {', '.join(known) or '(nenhum)'}."
        )
    out: dict = {}
    for name, fspec in known.items():
        value = raw.get(name)
        if value is None:
            if fspec.required:
                raise FieldError(f"o campo '{fspec.label}' é obrigatório para o tipo '{kind}'.")
            continue
        if fspec.type == "number":
            try:
                out[name] = int(str(value).strip())
            except (TypeError, ValueError):
                raise FieldError(f"o campo '{fspec.label}' precisa ser um número.") from None
            continue
        text = str(value).strip()
        if len(text) > MAX_FIELD_LEN:
            raise FieldError(f"o campo '{fspec.label}' passa de {MAX_FIELD_LEN} caracteres.")
        out[name] = text
    return out


def describe(kind: str, fields: dict | None) -> str:
    """Resumo de uma linha dos campos — para o painel e para o catálogo do cérebro."""
    spec = SPEC_BY_KIND.get(kind)
    if spec is None or not fields:
        return ""
    labels = {f.name: f.label.lower() for f in spec.fields}
    return " · ".join(
        f"{labels.get(k, k)}: {v}" for k, v in fields.items() if v not in (None, "")
    )
