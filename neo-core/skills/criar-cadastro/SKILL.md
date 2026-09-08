---
name: criar-cadastro
description: >
  Cria um cadastro novo (tabela + tela no painel) quando o usuário quer guardar
  um tipo de registro que ainda não existe — produtos, clientes, pedidos — ou
  acrescenta campo a um cadastro existente.
keywords: [criar cadastro, novo cadastro, cadastro de, cadastrar tipo, criar tabela, quero guardar, preciso registrar, campo novo, acrescentar campo, adicionar campo]
kind: tool_backed
discovery: keyword
tools: [data_define]
enabled: true
version: 1
---

# Criar um cadastro novo

## Quando usar
- "cria um cadastro de produtos com nome, preço e estoque"
- "preciso guardar meus clientes"
- "põe um campo de cor nos produtos"

NÃO use para gravar/consultar DADOS de um cadastro que já existe — isso é
`data_create`/`data_list`, e vem da skill do próprio cadastro.

## Como fazer

1. **Traduza o pedido em campos tipados.** Cada campo tem `name` (minúsculas com
   underscore), `type` e `label`. Tipos:
   `texto` · `texto_longo` · `inteiro` · `numero` · `dinheiro` · `booleano` ·
   `data` · `data_hora` · `opcao` (exige `options`) · `multi_opcao` ·
   `relacao` (exige `dataset`) · `arquivo` · `json`.

2. **Pergunte o que ficou ambíguo — ANTES de chamar a tool.** Trocar o tipo de
   um campo depois NÃO é automático (exige migração manual), então errar aqui
   custa caro. Perguntas que valem a pena:
   - valor em dinheiro? → `dinheiro` (aceita "49,90")
   - lista fechada de valores? → `opcao` com as opções dele
   - vai anexar foto/arquivo? → `arquivo`
   - aponta para outro cadastro (pedido → produto)? → `relacao`
   Não pergunte o óbvio: nome é `texto`, quantidade é `inteiro`.

3. **Chame `data_define`** com `slug`, `titulo`, `descricao` e `campos`.
   Para acrescentar campo a um cadastro existente: `modo="acrescentar"`.

4. **Confira o retorno.** Ele traz `conferido.colunas_no_banco`. Só diga que
   criou se as colunas que você pediu estiverem lá. Se não estiverem, relate o
   que voltou — não afirme sucesso.

5. **Diga ao usuário o que ele ganhou**: a tela já está no painel em
   `/dados/<slug>`, e ele pode cadastrar/consultar por lá ou falando com você.

## Regras
- `slug` no plural e em minúsculas: `produtos`, `clientes`, `ordens_servico`.
- Comece pequeno: 4 a 8 campos resolvem quase tudo. Campo novo é fácil de
  acrescentar depois; campo errado, não.
- NUNCA invente campo que o usuário não pediu nem citou. Se ele disse "nome,
  preço e estoque", são três campos — não acrescente "descrição" por conta.
- Um cadastro guarda REGISTRO de negócio (produto, cliente, pedido). Fato
  pessoal sobre o usuário continua indo para a memória (`pks_remember`), e
  dinheiro dele continua nas finanças (skill `financas`) — não recrie um
  controle financeiro pessoal aqui.
- Depois de criar, para o Neo usar o cadastro em conversa é preciso uma skill
  que declare `datasets: [<slug>]`. Avise o usuário disso: ele cria pela aba
  Skills do painel (é um SKILL.md curto) e depois clica em Instalar.
