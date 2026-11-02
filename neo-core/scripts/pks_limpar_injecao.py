"""Remove da memória do Neo o texto de terceiro que foi APRENDIDO como preferência.

Por que existe (medido em 2026-10-03): dois payloads de prompt injection que o
Daniel colou para TESTAR a defesa foram aprendidos pelo pipeline automático e
gravados dentro do `properties` de `neo-communication-preferences`,
`neo-personality` e `neo-response-rules` — blocos de ALTA PRIORIDADE que entram
em todo prompt. A injeção virou moradora permanente do contexto. O portão não viu
porque o aprendizado automático não é tool (ver release-request-plan §23).

Cirúrgico de propósito: o texto envenenado vive DENTRO de listas de um JSON junto
com a persona de verdade. Apagar o campo apagaria a personalidade dele. Este
script remove só os ITENS que casam uma marca, e nunca o campo.

    docker compose exec neo-core python3 scripts/pks_limpar_injecao.py            # dry-run
    docker compose exec neo-core python3 scripts/pks_limpar_injecao.py --aplicar

Imprime só o PREFIXO do que vai sair: o resto do bloco é conteúdo pessoal e não
precisa aparecer em log nenhum para a conferência ser possível.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from db.async_client import AsyncPostgresClient  # noqa: E402
from shared.config import get_settings  # noqa: E402

#: Marcas do payload de teste. Específicas de propósito: "ASSISTENTE" sozinho
#: casaria a regra legítima da persona ("evitar descrições como assistente
#: pessoal"), e aí a limpeza comeria a personalidade.
MARCAS = (
    "instrução ao assistente",
    "instrucao ao assistente",
    "processamento automatizado",
    "portal-regularize",
    "884512",
    "regularização cadastral",
    "regularizacao cadastral",
)

PREFIXO = 90


def envenenado(valor: object) -> bool:
    if not isinstance(valor, str):
        return False
    baixo = valor.lower()
    return any(m in baixo for m in MARCAS)


def limpar(no: object) -> tuple[object, list[str]]:
    """Devolve ``(estrutura limpa, itens removidos)``. Não mexe no que não casa."""
    removidos: list[str] = []
    if isinstance(no, list):
        saida = []
        for item in no:
            if envenenado(item):
                removidos.append(str(item))
                continue
            novo, saiu = limpar(item)
            removidos.extend(saiu)
            saida.append(novo)
        return saida, removidos
    if isinstance(no, dict):
        saida_d = {}
        for chave, valor in no.items():
            if envenenado(valor):
                removidos.append(f"{chave}: {valor}")
                continue
            novo, saiu = limpar(valor)
            removidos.extend(saiu)
            saida_d[chave] = novo
        return saida_d, removidos
    return no, removidos


async def main() -> int:
    aplicar = "--aplicar" in sys.argv
    db = AsyncPostgresClient(get_settings().database_url)
    total_itens = 0
    total_campos = 0

    async with db.get_session() as session:
        linhas = (
            await session.execute(
                text(
                    "select f.id, e.name, f.key, f.value_json "
                    "from pks_entity_field f join pks_entity e on e.id = f.entity_id "
                    "where f.value_type = 'json' and f.value_json is not null "
                    "order by e.name, f.key"
                )
            )
        ).all()

        for campo_id, entidade, chave, bruto in linhas:
            atual = bruto if isinstance(bruto, (dict, list)) else json.loads(bruto or "null")
            if atual is None:
                continue
            limpo, removidos = limpar(atual)
            if not removidos:
                continue
            total_campos += 1
            total_itens += len(removidos)
            print(f"\n{entidade}.{chave}  ({len(removidos)} item(ns) a remover)")
            for item in removidos:
                uma_linha = " ".join(item.split())
                print(f"  - {uma_linha[:PREFIXO]}…")
            if aplicar:
                await session.execute(
                    text(
                        "update pks_entity_field set value_json = cast(:valor as jsonb) "
                        "where id = :id"
                    ),
                    {"valor": json.dumps(limpo, ensure_ascii=False), "id": campo_id},
                )
        if aplicar and total_itens:
            await session.commit()

    print(
        f"\n{total_itens} item(ns) em {total_campos} campo(s)."
        + (" APLICADO." if aplicar else " Dry-run — nada mudou. Use --aplicar.")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
