"""Caminhos que só existem na MÁQUINA DO USUÁRIO, nunca no sandbox.

O sandbox (`/workspace`) é um container Linux na VPS. O disco do usuário é outra
máquina, sem filesystem compartilhado. Um comando de sandbox que cita
`/Users/daniel/Desktop/...` não "às vezes funciona": ele falha SEMPRE, e falha
tarde — depois de virar job, entrar na fila e queimar um ciclo do dispatcher.

Sessão d9f9417b (2026-09-20): o trilho SERVER_TOOL respondeu "cria a pasta, voce
vai ter que baixar os pdfs na internet" abrindo três `sandbox_task`::

    mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais
      → mkdir: cannot create directory '/Users': Permission denied   (×2)
    ls -la /Users/daniel/Desktop/teste-de-analise/relatorios-anuais/
      → No such file or directory

Nenhum dos três apareceu no log da sessão — só no banco. Do lado do usuário, o
Neo simplesmente não fez nada.

A guarda é estreita de propósito. Só entram layouts que o container comprovadamente
não tem e que nenhuma ferramenta do sandbox produz:

- ``/Users/…``          — layout de macOS; o container é Linux e não tem `/Users`.
- ``C:\\Users\\…``       — Windows.
- ``~/Desktop`` e irmãs — o `~` do container é o home do root, sem essas pastas.

O que NÃO entra: `/home/…` (o container pode ter), `/tmp`, `/workspace`, caminho
relativo. Barrar comando bom é pior que deixar passar um ruim.
"""

from __future__ import annotations

import re

# `/Users/<algo>` e `C:\Users\<algo>` / `C:/Users/<algo>`.
_USER_ROOT_RE = re.compile(r"(?:(?<![\w/])/Users/|[A-Za-z]:[\\/]Users[\\/])[A-Za-z0-9._-]+")

# `~/Desktop`, `~/Downloads`, … — pastas de usuário de desktop que o home do root
# no container não tem. `~/algumacoisa` genérico NÃO conta.
_HOME_DESKTOP_RE = re.compile(
    r"~/(?:Desktop|Downloads|Documents|Movies|Music|Pictures|Library)\b"
)

#: O que dizer ao modelo quando ele tenta. A mensagem é metade do conserto: sem
#: ela o modelo repete o mesmo comando (foram três tentativas na sessão).
REFUSAL = (
    "Este comando cita um caminho da MÁQUINA DO USUÁRIO ({achados}), e você está "
    "no sandbox — um container Linux na VPS, sem acesso ao disco dele. Rodar isso "
    "aqui falha sempre. Se o trabalho é no disco DELE, o sandbox não é o caminho: "
    "produza o arquivo aqui em /workspace, publique com `file_save` e entregue com "
    "`save_generated_file` (é a única ponte). Se o que você queria era só um lugar "
    "para trabalhar, use /workspace."
)


def user_machine_paths(command: str) -> list[str]:
    """Trechos de ``command`` que apontam para o disco do usuário (vazio = ok)."""
    if not command:
        return []
    achados = _USER_ROOT_RE.findall(command) + _HOME_DESKTOP_RE.findall(command)
    # dedup preservando ordem
    vistos: set[str] = set()
    out: list[str] = []
    for a in achados:
        if a not in vistos:
            vistos.add(a)
            out.append(a)
    return out


def refusal_for(command: str) -> str | None:
    """Mensagem de recusa, ou ``None`` quando o comando não toca o disco dele."""
    achados = user_machine_paths(command)
    if not achados:
        return None
    return REFUSAL.format(achados=", ".join(repr(a) for a in achados[:3]))
