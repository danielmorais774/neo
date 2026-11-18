---
name: nota-fiscal
description: >
  Ler a FOTO de uma nota fiscal ou cupom (mercado, loja), conferir as contas e
  montar planilha. Use para OCR de notinha/cupom e para dizer total ou itens dela.
keywords: [notinha, nota fiscal, notas fiscais, cupom fiscal, nfce, nfc-e, danfe, nota do mercado, notinha do mercado, nota de supermercado, nota do supermercado, comprovante de compra]
kind: tool_backed
register: oficina
tools: [conferir_cupom]
enabled: true
version: 1
---

# Nota fiscal / cupom: ler, conferir, entregar

## Quando usar
- Foto (JPG/PNG) de nota fiscal ou cupom de compra que ele mandou, anexou ou
  encaminhou por e-mail, e ele quer os itens, o total ou uma planilha.
- NÃO é vídeo: a skill `ocr-video` não serve aqui, e tesseract em foto de cupom
  térmico volta ilegível. Não rode.

## Como fazer
1. **A imagem no sandbox.** Anexo de e-mail: `email_anexo` e depois `file_stage`.
   Arquivo do catálogo: `file_stage`.
2. **Leia a nota INTEIRA uma vez** com `file_olhar` na imagem original. Se ela
   passar de 4 MB, reduza com PIL antes, sem recortar. Pergunta:
   "Transcreva cada linha de item (código, descrição, quantidade, unidade, valor
   unitário, valor total), o valor a pagar, a quantidade total de itens, cada forma
   de pagamento com o valor e o troco. Linha ilegível: diga 'ilegível'."
3. **Confira as contas com `conferir_cupom`** antes de qualquer outra coisa:
   passe os itens exatamente como foram lidos (valores em texto, "39,99") e os
   totais da nota. Não faça a conta de cabeça.
   - `todas_as_linhas_lidas: true` → a leitura está completa. NÃO releia a nota
     em faixas nem recortes.
   - `correcoes` com confiança `alta` → aplique a correção (o valor `para`). Ela
     vem de duas provas que concordam.
   - Correção `media` ou `baixa`, ou "nenhuma leitura isolada explica" → recorte
     SÓ as linhas apontadas (com folga acima e abaixo), olhe de novo com
     `file_olhar` e confira de novo. No máximo duas vezes; depois disso, entregue
     com a linha marcada como incerta.
4. **Monte o arquivo com os valores conferidos** (`neo_bash` + openpyxl): uma
   linha por item, na ordem da nota, com fórmula `=SUM(...)` no total e as formas
   de pagamento abaixo. Entregue com `file_save`. Se o `file_save` voltar com
   "Conferência da planilha", corrija e salve de novo.
5. **Na resposta:** o total, quantos itens, e o que foi corrigido ou ficou
   incerto ("a vodka saiu 33,99 na leitura; a conta mostra 39,99"). Não diga que
   as contas fecham se a `conferir_cupom` não disse isso.
6. **Apague a foto e os recortes** de `/workspace/files/` depois de entregar.

## Regras
- O valor de cada linha é o que a NOTA diz, corrigido só pela `conferir_cupom`
  ou por uma releitura. Não invente desconto para fazer a conta fechar.
- Itens repetidos na nota (dois "GELO 3kg") são duas linhas, não uma leitura
  dobrada. A quantidade total de itens da nota confirma.
