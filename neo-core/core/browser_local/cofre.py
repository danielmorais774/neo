"""Conta e senha no navegador local, pelo cofre (F5 de docs/browser-conversa-plan.md).

Job 02f46eff (12/11): o Neo criou uma conta no AppDemo e a senha se perdeu. Quem a
inventou foi o cérebro, no texto da tarefa ("senha provisória Mes@2026teste"),
e ela passou em claro pelo prompt, pelo caderno, pela leitura da página (o
snapshot mostrava o VALOR do campo de senha) e pela conferência. Nada foi para
o cofre. O e-mail também foi inventado: uma caixa pública do mailinator, que
qualquer pessoa lê.

Aqui a senha deixa de passar pelo modelo:

* conta NOVA: o agente escreve ``«senha:nova»`` no campo de senha (e no de
  confirmar). Quem gera é o código, uma por site por tarefa, e ela vai para o
  cofre ANTES de ir para a página — conta cuja senha se perdeu é pior que conta
  não criada;
* conta GUARDADA: ``«cofre:rótulo»``. O segredo só sai para o domínio da
  credencial (mesma regra do ``core/credentials/browser.py``), e só para campo
  de senha;
* senha escrita à mão num campo de senha é recusada, e caixa de e-mail pública
  também, quando o cofre tem o e-mail do Neo para oferecer no lugar.
"""

from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

MARCA_NOVA = "«senha:nova»"
#: ``«senha:nova»`` ou ``«cofre:rótulo»`` — o valor inteiro do `fill` é o marcador.
_MARCA = re.compile(r"«\s*(senha\s*:\s*nova|cofre\s*:\s*([^»]{1,120}?))\s*»", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@((?:[\w-]+\.)+[\w-]{2,})", re.UNICODE)
#: Caixas de e-mail PÚBLICAS: qualquer pessoa lê a confirmação e troca a senha.
_CAIXAS_PUBLICAS = (
    "mailinator.com", "yopmail.com", "guerrillamail.", "sharklasers.com", "grr.la",
    "10minutemail.", "temp-mail.", "tempmail.", "maildrop.cc", "dispostable.com",
    "trashmail.", "getnada.com", "mailnesia.com", "mohmal.com", "emailondeck.com",
    "throwawaymail.com", "fakeinbox.com", "mintemail.com", "inboxkitten.com",
    "tempr.email", "mail.tm", "burnermail.io", "spamgourmet.com",
)
_SIMBOLOS = "!@#$%*-_"
_TAMANHO_DA_SENHA = 20
#: Quanto do rótulo cabe no cofre (o painel mostra em uma linha).
_ROTULO_MAX = 120
#: Nome do site (título da aba) na descrição da credencial.
_SITE_MAX = 80


def _agora_local() -> str:
    """Dia e hora no fuso do Daniel, para o rótulo que ele lê no painel."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m %H:%M")
    except Exception:  # noqa: BLE001 — sem tzdata, UTC dito
        return datetime.utcnow().strftime("%d/%m %H:%M UTC")


def marca_do_valor(valor: str) -> tuple[str, str] | None:
    """``("nova", "")``, ``("cofre", rótulo)`` ou ``None`` se o valor não é marcador."""
    achado = _MARCA.search(valor or "")
    if not achado:
        return None
    if achado.group(2):
        return "cofre", achado.group(2).strip()
    return "nova", ""


def tem_marca(valor: str) -> bool:
    return marca_do_valor(valor) is not None


def gerar_senha(tamanho: int = _TAMANHO_DA_SENHA) -> str:
    """Senha forte que passa nas regras comuns (maiúscula, minúscula, dígito, símbolo)."""
    alfabeto = string.ascii_letters + string.digits + _SIMBOLOS
    while True:
        senha = "".join(secrets.choice(alfabeto) for _ in range(tamanho))
        if (
            any(c.islower() for c in senha)
            and any(c.isupper() for c in senha)
            and any(c.isdigit() for c in senha)
            and any(c in _SIMBOLOS for c in senha)
        ):
            return senha


def dominio_limpo(dominio: str) -> str:
    """'https://*.mes.com.br/x' → 'mes.com.br'."""
    valor = (dominio or "").strip().lower()
    valor = re.sub(r"^[a-z*]+://", "", valor)
    valor = valor.split("/")[0].split(":")[0]
    return valor.lstrip("*.").strip()


def host_do_dominio(host: str, dominio: str) -> bool:
    """O host da aba é o domínio da credencial (ou um subdomínio dele)?"""
    alvo = dominio_limpo(dominio)
    host = (host or "").strip().lower().split(":")[0]
    return bool(alvo) and bool(host) and (host == alvo or host.endswith("." + alvo))


def caixa_publica(valor: str) -> str:
    """O domínio da caixa pública no valor, ou ``""``."""
    for achado in _EMAIL.finditer(valor or ""):
        dominio = achado.group(1).lower()
        for publica in _CAIXAS_PUBLICAS:
            if dominio == publica.rstrip(".") or dominio.startswith(publica) or dominio.endswith(
                "." + publica.rstrip(".")
            ):
                return dominio
    return ""


def parece_login(valor: str) -> bool:
    """O valor é um e-mail (o login de quase todo cadastro)?"""
    return bool(_EMAIL.fullmatch((valor or "").strip()))


class Cofre:
    """O cofre visto de UMA tarefa do navegador.

    ``store`` é o ``CredentialStore`` (ou ``None``: sem ``NEO_CRED_KEY`` não há
    cofre, e aí conta nova é recusada em vez de criada com senha que se perde).
    """

    def __init__(self, store: Any, *, job_id: Any = "", titulo: str = "") -> None:
        self._store = store
        self._job = str(job_id or "")[:8]
        self._titulo = str(titulo or "")[:120]
        #: host → senha gerada NESTA tarefa (o "confirmar senha" é a mesma).
        self._novas: dict[str, str] = {}
        #: host → (id da credencial, rótulo, usuário) do que esta tarefa guardou.
        self._guardadas: dict[str, tuple[str, str, str]] = {}
        #: Frases para o fim da tarefa: o que foi para o cofre.
        self.relato: list[str] = []
        self._logins: dict[str, list[Any]] = {}
        self._emails: list[str] | None = None

    @property
    def disponivel(self) -> bool:
        return self._store is not None

    async def emails_do_neo(self) -> list[str]:
        """Os endereços das caixas de e-mail do cofre (endereço é público)."""
        if self._emails is not None:
            return self._emails
        enderecos: list[str] = []
        if self._store is not None:
            try:
                for info in await self._store.list_by_kind("email"):
                    endereco = str((info.fields or {}).get("address") or "").strip()
                    if endereco and endereco not in enderecos:
                        enderecos.append(endereco)
            except Exception as exc:  # noqa: BLE001 — sem lista, sem sugestão
                logger.warning("[cofre] não consegui listar as caixas: %s", exc)
        self._emails = enderecos
        return enderecos

    async def logins_do_site(self, host: str) -> list[Any]:
        """Credenciais de login cujo domínio é este site, a mais nova primeiro."""
        host = (host or "").lower()
        if not host or self._store is None:
            return []
        if host not in self._logins:
            achados: list[Any] = []
            try:
                for info in await self._store.list():
                    if info.kind not in ("password", "basic"):
                        continue
                    if host_do_dominio(host, str((info.fields or {}).get("domain") or "")):
                        achados.append(info)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[cofre] não consegui listar logins de %s: %s", host, exc)
            achados.sort(key=lambda i: i.updated_at or i.created_at or datetime.min, reverse=True)
            self._logins[host] = achados
        return self._logins[host]

    def tem_senha_nova(self, host: str) -> bool:
        return (host or "").lower() in self._novas

    async def senha_nova(self, host: str, usuario: str = "", site: str = "") -> tuple[str, str]:
        """A senha da conta nova neste site: ``(senha, rótulo no cofre)``.

        Uma por site por tarefa — a segunda chamada (o "confirmar senha", ou uma
        nova tentativa do cadastro) devolve a mesma. Vai para o cofre ANTES de
        voltar: se o cofre falhar, levanta, e o campo fica vazio.
        """
        host = (host or "").lower()
        if self._store is None:
            raise RuntimeError(
                "o cofre está desligado (NEO_CRED_KEY), e conta nova sem cofre perde a senha. "
                "Não crie a conta: pergunte ao Daniel"
            )
        if host in self._novas:
            await self.anotar_usuario(host, usuario)
            return self._novas[host], self._guardadas[host][1]
        senha = gerar_senha()
        rotulo = await self._guardar(host, usuario, senha, site)
        self._novas[host] = senha
        return senha, rotulo

    async def _guardar(self, host: str, usuario: str, senha: str, site: str = "") -> str:
        """Cria a credencial. NUNCA sobrescreve: rótulo repetido ganha sufixo.

        Atualizar uma credencial que já existe trocaria a senha de uma conta que
        funciona (a do Daniel, ou a que outra tarefa criou) pela de um cadastro
        que talvez nem tenha passado.
        """
        base = f"{host} · {usuario or 'conta nova'} · Neo {_agora_local()}"[:_ROTULO_MAX]
        curto = base[: _ROTULO_MAX - 12]
        candidatos = [base, f"{curto} ({self._job})" if self._job else "", f"{curto} #{secrets.token_hex(2)}"]
        # O nome do site (título da aba) entra na descrição porque o rótulo só
        # tem o domínio, e é pelo nome que o Daniel e o cérebro procuram: na
        # sessão a20f4722, `vault_list("appdemo")` não achou a conta do AppDemo, que
        # estava guardada como sistema-exemplo.vercel.app.
        nome = " ".join((site or "").split())[:_SITE_MAX]
        descricao = (
            (f"Login do site «{nome}» ({host}). " if nome else "")
            + f"Conta criada pelo Neo na tarefa do navegador {self._job}"
            + (f" («{self._titulo}»)" if self._titulo else "")
            + ". Senha gerada pelo sistema — o modelo nunca a viu. Se o cadastro não "
            "foi concluído, pode apagar."
        )
        campos = {"username": usuario or "(não identificado)", "domain": host}
        info = None
        for rotulo in [c for c in candidatos if c]:
            try:
                info = await self._store.create(
                    label=rotulo, secret=senha, kind="password", description=descricao,
                    fields=campos,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "code", "") == "duplicate_label":
                    continue
                raise RuntimeError(
                    f"não consegui guardar a senha no cofre ({exc}); conta nova sem cofre perde "
                    "a senha. Não crie a conta: conte ao Daniel"
                ) from exc
        if info is None:
            raise RuntimeError(
                "não consegui um rótulo livre no cofre para a senha nova. Não crie a conta: "
                "conte ao Daniel"
            )
        self._guardadas[host] = (str(info.id), rotulo, usuario)
        self._logins.pop(host, None)
        self.relato.append(
            f"Guardei no cofre a senha da conta nova em {host}: «{rotulo}»"
            + (f" (usuário {usuario})" if usuario else "")
            + "."
        )
        logger.info("[cofre] senha nova guardada para %s (job %s)", host, self._job)
        return rotulo

    async def anotar_usuario(self, host: str, usuario: str) -> None:
        """O login apareceu depois da senha: completa a credencial que esta tarefa criou."""
        host = (host or "").lower()
        guardada = self._guardadas.get(host)
        if not guardada or not usuario or guardada[2] or self._store is None:
            return
        cid, rotulo, _ = guardada
        try:
            await self._store.update(cid, fields={"username": usuario, "domain": host})
        except Exception as exc:  # noqa: BLE001 — o rótulo já diz o site
            logger.warning("[cofre] não consegui anotar o usuário de %s: %s", host, exc)
            return
        self._guardadas[host] = (cid, rotulo, usuario)
        self.relato = [
            (f"{r[:-1]} (usuário {usuario})." if f"«{rotulo}»" in r and "usuário" not in r else r)
            for r in self.relato
        ]

    async def senha_guardada(self, rotulo: str, host: str) -> str:
        """O segredo de ``«cofre:rótulo»``, só para o domínio da credencial."""
        if self._store is None:
            raise RuntimeError("o cofre está desligado neste Neo (NEO_CRED_KEY)")
        try:
            achado = await self._store.resolve(rotulo)
        except Exception as exc:  # noqa: BLE001 — chave trocada etc.
            raise RuntimeError(f"não consegui abrir «cofre:{rotulo}»: {exc}") from exc
        if achado is None:
            raise RuntimeError(
                f"não existe «cofre:{rotulo}». Use um rótulo da lista COFRE do prompt, ou "
                "pergunte ao Daniel"
            )
        info, segredo = achado
        if info.kind not in ("password", "basic"):
            raise RuntimeError(f"«cofre:{rotulo}» não é um login de site (é {info.kind})")
        dominio = str((info.fields or {}).get("domain") or "")
        if not dominio:
            raise RuntimeError(
                f"«cofre:{rotulo}» não tem domínio de uso — sem ele a senha valeria em "
                "qualquer página. O Daniel precisa preencher o domínio no neo-panel"
            )
        if not host_do_dominio(host, dominio):
            # A página não escolhe para onde a senha vai: é a defesa contra uma
            # página pedir "digite a senha do banco aqui".
            raise RuntimeError(
                f"«cofre:{rotulo}» vale para {dominio_limpo(dominio)}, e esta aba é {host}. "
                "A senha não sai do site dela"
            )
        try:
            await self._store.touch(info.id)
        except Exception:  # noqa: BLE001
            pass
        return segredo

    async def bloco_do_prompt(self, host: str) -> str:
        """O que o agente precisa saber numa página que pede senha (sem segredo)."""
        linhas = ["COFRE (esta página tem campo de senha — senha você NUNCA escreve):"]
        emails = await self.emails_do_neo()
        if emails:
            linhas.append(
                f"- conta NOVA: e-mail = {emails[0]} (o do Neo, liberado pelo Daniel para "
                "cadastros); senha = `«senha:nova»` no campo de senha E no de confirmar — "
                "eu gero uma forte, digito e guardo no cofre"
            )
        else:
            linhas.append(
                "- conta NOVA: senha = `«senha:nova»` no campo de senha E no de confirmar — "
                "eu gero, digito e guardo no cofre. E-mail: o que a tarefa ou o Daniel deu"
            )
        logins = await self.logins_do_site(host)
        if logins:
            linhas.append(f"- logins guardados para {host} (para ENTRAR, a senha é o marcador):")
            for info in logins[:4]:
                usuario = str((info.fields or {}).get("username") or "").strip()
                linhas.append(
                    f"  · `«cofre:{info.label}»`" + (f" — usuário {usuario}" if usuario else "")
                )
        else:
            linhas.append(f"- nenhum login guardado para {host}")
        return "\n".join(linhas)
