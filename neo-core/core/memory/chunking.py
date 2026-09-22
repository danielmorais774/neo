"""Fatiamento de mensagens longas para embedding.

O embedder local (`all-MiniLM-L6-v2`) tem `max_seq_length = 256` tokens e
trunca em SILÊNCIO — sem erro, sem warning, sem marcador. O vetor de uma
mensagem longa representa só a abertura dela, e o resto fica invisível para a
busca vetorial. Ver `db/models/conversation.py::ConversationMessageChunk`.
"""

from __future__ import annotations

_BREAKS = ("\n\n", "\n", ". ", " ")


def chunk_text(text: str, size: int = 1200, overlap: int = 300) -> list[str]:
    """Fatia em pedaços de ~``size`` chars com ``overlap`` de sobreposição.

    Corta preferencialmente numa quebra de linha ou fim de frase dentro da
    última quarta parte do pedaço: cortar no meio de uma linha de tabela
    markdown desmancha justamente o que a busca precisa casar. A sobreposição
    cobre o caso de a resposta cair bem na emenda entre dois pedaços.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    if overlap >= size:
        overlap = size // 4

    pedacos: list[str] = []
    inicio = 0
    while inicio < len(text):
        fim = inicio + size
        if fim >= len(text):
            pedacos.append(text[inicio:].strip())
            break
        # Procura um corte natural no último quarto da janela.
        janela_min = inicio + (size * 3 // 4)
        corte = -1
        for marca in _BREAKS:
            pos = text.rfind(marca, janela_min, fim)
            if pos > 0:
                corte = pos + len(marca)
                break
        if corte <= inicio:
            corte = fim
        pedaco = text[inicio:corte].strip()
        if pedaco:
            pedacos.append(pedaco)
        proximo = corte - overlap
        # Sem isto, um corte natural muito cedo faria o laço andar para trás.
        inicio = proximo if proximo > inicio else corte

    return [p for p in pedacos if p]
