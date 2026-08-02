"""PKS write tool — escrita sob demanda no PKS (docs/pks-write-on-demand-plan.md).

`pks_remember` é a ÚNICA tool com efeito colateral no read set do cognitivo: o
Neo a chama quando o usuário pede EXPLICITAMENTE para lembrar/guardar algo. A
tool é fina de propósito — não recebe tipo de entidade nem campos; quem decide
COMO/ONDE estruturar é o subagente de memória (PKSMemoryPlanner) por baixo. O
cognitivo só decide O QUÊ (resolve "essa ideia"/"isso" e formula o conteúdo).
"""

from shared import ToolParameter, ToolSchema

PKS_WRITE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="pks_remember",
        description=(
            "Salva um fato/ideia na memória permanente do usuário (PKS) QUANDO ELE "
            "PEDIR explicitamente para lembrar/guardar/anotar algo ('salva isso', "
            "'lembra dessa ideia', 'anota que...', 'guarda pra depois'). Passe em "
            "`content` o fato JÁ formulado por você em linguagem natural, resolvendo "
            "qualquer referência ('essa ideia' → escreva a ideia por extenso). Um "
            "subagente decide sozinho o tipo/lugar certo na memória — você NÃO informa "
            "entidade nem campos. Depois de chamar, confirme ao usuário citando o que "
            "foi salvo (a tool devolve um resumo). NUNCA diga que salvou algo sem ter "
            "chamado esta tool nesta mesma resposta. Não a use para conversa comum, "
            "nem para dinheiro/senha (esses têm canais próprios)."
        ),
        module="pks_write",
        execution="server-side",
        timeout=30,
        parameters={
            "content": ToolParameter(
                type="string",
                description=(
                    "O fato/ideia a lembrar, escrito por extenso e auto-contido (sem "
                    "'isso'/'aquilo' pendurados). Ex.: 'Bebida pré-treino sem cafeína "
                    "preferida: suco de uva integral 1/3 + água com gás 2/3, gelado.'"
                ),
                required=True,
            ),
            "note": ToolParameter(
                type="string",
                description=(
                    "Opcional: contexto do pedido do usuário (ex.: 'pra quando ele "
                    "pedir bebida pré-treino de novo'). Ajuda a recuperar depois."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="pks_forget",
        description=(
            "Remove/esquece um fato da memória permanente do usuário (PKS) QUANDO "
            "ELE PEDIR explicitamente ('esquece isso', 'apaga aquela anotação', "
            "'não quero mais que você lembre de X', 'tira da memória'). Passe em "
            "`target` a descrição — por extenso, resolvendo 'isso'/'aquilo' — do "
            "que deve ser removido. Um subagente localiza a entidade certa na "
            "memória e a apaga (soft delete, reversível). Se nada casar, a tool "
            "avisa que não achou — NUNCA diga que apagou sem ter chamado esta "
            "tool nesta mesma resposta, e confirme citando o que foi removido "
            "(a tool devolve um resumo). Para CORRIGIR um valor (em vez de apagar), "
            "use `pks_remember` — ele sobrescreve o dado existente."
        ),
        module="pks_write",
        execution="server-side",
        timeout=30,
        parameters={
            "target": ToolParameter(
                type="string",
                description=(
                    "O que esquecer, por extenso e auto-contido (sem 'isso'/'aquilo' "
                    "pendurados). Ex.: 'a bebida pré-treino de suco de uva' ou "
                    "'minha preferência de bebida pré-treino sem cafeína'."
                ),
                required=True,
            ),
        },
    ),
]
