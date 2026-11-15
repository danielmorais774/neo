"""Tira da memória do Neo os resultados de job que viraram "exemplo de estilo".

Por que existe (session-20cb02b6, 09/11/2026): o bloco de Alta Prioridade
`neo-communication-preferences` carregava, em `examples`, sete mensagens de
continuação de job ("[Continuação automática — não é mensagem do usuário…] Job:
'Extrair questões do Portal para .txt'… Q1–Q10 extraídas…"). A continuação entra
no laço como se fosse o usuário falando e o extrator de política de comunicação a
guardou. Esse conteúdo ia em TODO prompt; numa conversa nova sobre um template de
outra disciplina, o Neo foi buscar aquelas questões. A causa foi fechada em
`PKSService._mensagem_sintetica`; este script limpa o que já tinha entrado.

Mesmo critério cirúrgico do `pks_limpar_injecao.py`: remove só os ITENS que
começam com a marca da continuação, nunca o campo inteiro. Campo de texto que é
inteiro uma continuação é apagado com `deleted_at` (soft delete, igual ao resto do
PKS).

    docker compose exec neo-core python3 scripts/pks_limpar_exemplos_de_job.py            # dry-run
    docker compose exec neo-core python3 scripts/pks_limpar_exemplos_de_job.py --aplicar

Imprime só o PREFIXO do que vai sair.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARCA = "[continuação automática"
PREFIXO = 90


def sintetico(valor: object) -> bool:
    return isinstance(valor, str) and valor.lstrip().lower().startswith(MARCA)


def limpar(no: object) -> tuple[object, list[str]]:
    """Devolve ``(estrutura limpa, itens removidos)``. Não mexe no que não casa."""
    removidos: list[str] = []
    if isinstance(no, list):
        saida = []
        for item in no:
            if sintetico(item):
                removidos.append(str(item))
                continue
            novo, saiu = limpar(item)
            removidos.extend(saiu)
            saida.append(novo)
        return saida, removidos
    if isinstance(no, dict):
        saida_d = {}
        for chave, valor in no.items():
            if sintetico(valor):
                removidos.append(f"{chave}: {valor}")
                continue
            novo, saiu = limpar(valor)
            removidos.extend(saiu)
            saida_d[chave] = novo
        return saida_d, removidos
    return no, removidos


async def main() -> int:
    from sqlalchemy import text

    from db.async_client import AsyncPostgresClient
    from shared.config import get_settings

    aplicar = "--aplicar" in sys.argv
    db = AsyncPostgresClient(get_settings().database_url)
    total_itens = 0

    async with db.get_session() as session:
        linhas = (
            await session.execute(
                text(
                    "select f.id, e.name, f.key, f.value_type, f.value_json, f.value_text "
                    "from pks_entity_field f join pks_entity e on e.id = f.entity_id "
                    "where f.deleted_at is null and e.deleted_at is null "
                    "and (f.value_json is not null or f.value_text is not null) "
                    "order by e.name, f.key"
                )
            )
        ).all()

        for campo_id, entidade, chave, tipo, bruto_json, bruto_texto in linhas:
            if bruto_json is not None:
                atual = bruto_json if isinstance(bruto_json, (dict, list)) else json.loads(bruto_json or "null")
                limpo, removidos = limpar(atual)
                if not removidos:
                    continue
                total_itens += len(removidos)
                print(f"\n{entidade}.{chave}  ({len(removidos)} item(ns) a remover da lista)")
                for item in removidos:
                    print(f"  - {' '.join(item.split())[:PREFIXO]}…")
                if aplicar:
                    await session.execute(
                        text("update pks_entity_field set value_json = cast(:valor as jsonb), "
                             "updated_at = now() where id = :id"),
                        {"valor": json.dumps(limpo, ensure_ascii=False), "id": campo_id},
                    )
            elif sintetico(bruto_texto):
                total_itens += 1
                print(f"\n{entidade}.{chave}  (campo de texto inteiro é continuação — soft delete)")
                print(f"  - {' '.join(str(bruto_texto).split())[:PREFIXO]}…")
                if aplicar:
                    await session.execute(
                        text("update pks_entity_field set deleted_at = now(), updated_at = now() where id = :id"),
                        {"id": campo_id},
                    )
        if aplicar and total_itens:
            await session.commit()

    print(f"\n{total_itens} item(ns)." + (" APLICADO." if aplicar else " Dry-run — nada mudou. Use --aplicar."))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
