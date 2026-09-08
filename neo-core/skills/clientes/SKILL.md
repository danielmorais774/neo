---
name: clientes
description: 'Clientes da loja: cadastrar, achar por nome/telefone, ver histórico de contato. Use quando falarem de cliente, comprador ou contato de venda.'
keywords: [cliente, clientes, comprador, contato, telefone do cliente]
kind: tool_backed
group: cadastros
discovery: keyword
datasets: [{slug: clientes, titulo: Clientes, descricao: Quem compra na loja, campos: [{name: nome, type: texto, label: Nome, required: true, max: 120}, {name: telefone, type: texto, label: Telefone, max: 30}, {name: email, type: texto, label: E-mail, max: 120}, {name: origem, type: opcao, label: Origem, options: [whatsapp, instagram, indicacao, loja]}, {name: observacoes, type: texto_longo, label: Observações}]}]
enabled: true
version: 1
---

# Clientes

## Como fazer
- Achar: `data_list(dataset="clientes", busca="...")`.
- Cadastrar: `data_create(dataset="clientes", campos={...})` — só afirme que cadastrou se vier `conferido`.
- Nunca invente telefone ou e-mail.
