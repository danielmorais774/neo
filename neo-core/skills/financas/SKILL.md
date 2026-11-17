---
name: financas
description: >
  Dinheiro do usuário: registrar gasto/receita (Pix, débito, cartão, fatura,
  parcela), extrato, saldo, orçamento. Use quando ele contar que
  gastou/pagou/comprou/recebeu ou perguntar quanto gastou/tem.
keywords: [gastei, gasto, gastos, paguei, comprei, recebi, salario, salário, pix, cartao, cartão, cartoes, cartões, fatura, faturas, parcela, parcelas, parcelado, parcelamento, boleto, extrato, saldo, saldos, carteira, despesa, despesas, lancamento, lançamento, lancamentos, lançamentos, financas, finanças, financeiro, orçamento, orcamento, quanto gastei, quanto tenho, quanto devo]
kind: tool_backed
register: oficina
tools: [fin_add_transaction, fin_transfer, fin_statement, fin_card_invoice, fin_balances, fin_insights, fin_manage, fin_edit_transaction]
enabled: true
version: 1
---

# Finanças pessoais (BR: Pix, cartão, fatura, parcelamento)

## Quando usar
- Usuário conta um gasto/receita: "gastei 45 no mercado", "paguei 120 de luz no
  Pix", "recebi o salário", "comprei uma TV em 10x no Nubank".
- Perguntas sobre dinheiro dele: "quanto gastei esse mês?", "quanto tá a fatura
  do Nubank?", "quanto tenho na conta?", "onde tô gastando mais?".
- Cadastro: contas, cartões, categorias, orçamentos ("me avisa se eu passar de
  800 com comida"), assinaturas/contas fixas.
- NÃO use para cotações/investimentos de mercado (use web_search).

## Conceitos (como o dinheiro flui aqui)
- **Conta** = onde o dinheiro mora (corrente, poupança, carteira=dinheiro vivo,
  vale VR/VA). **Cartão** = só crédito; débito é `payment_method=debito` numa conta.
- **Crédito**: a compra NÃO sai da conta — entra na FATURA do cartão (mês
  calculado pelo dia de fechamento; compra no dia do fechamento ou depois cai na
  fatura seguinte). Quem debita a conta é o PAGAMENTO da fatura (`fin_transfer`
  com `card`) — e isso não é despesa (a despesa foi a compra), então não duplica.
- **Parcelamento**: `installments=N` cria N parcelas, uma por fatura; cada
  parcela conta como gasto no MÊS da sua fatura (visão de fluxo de caixa), com
  ajuste de centavos na 1ª. O extrato mostra "parc 2/10".
- **Transferência/saque/depósito** entre contas: `fin_transfer` (não é gasto).

## Fluxos
1. **Gasto rápido**: `fin_add_transaction(kind=despesa, amount="45,90",
   description="mercado", category="Mercado", payment_method="pix")` — conta
   padrão é usada se não disser qual. Sempre tente categorizar (invente pela
   descrição: mercado→Mercado, uber→App de transporte, ifood→Delivery).
2. **Crédito**: inclua `card="Nubank"`. Parcelado: `installments=10`.
3. **Pagar fatura**: `fin_transfer(from_account="Carteira", card="Nubank")` — sem
   `invoice_month` pega a fatura aberta mais antiga. **OMITA `amount`** quando o
   usuário não disser o valor ("paguei a fatura"): a tool paga o total em aberto.
   Nunca chute o valor da fatura.
   - **Mês específico / adiantar futura**: quando o usuário disser QUAL fatura
     ("paga a de agosto", "adianta o próximo mês", "quita a fatura que vem"),
     passe `invoice_month` — aceita `AAAA-MM` OU relativo (`"proximo"`, `"atual"`,
     `"anterior"`). Você PODE pagar/adiantar uma fatura futura que tenha saldo em
     aberto — o sistema não te obriga a pagar só a corrente. Se o usuário falou de
     um mês, NUNCA deixe o default pagar a fatura mais antiga no lugar.
4. **Consultas**: extrato → `fin_statement`; fatura → `fin_card_invoice`;
   saldos/visão geral → `fin_balances`; análise → `fin_insights`.
5. **Corrigir/excluir/desfazer**: `fin_edit_transaction`. Se não tem o id, use
   `target` (ex.: desfazer pagamento → `{"kind":"pagamento_fatura","card":"Nubank",
   "invoice_month":"2026-07"}`). Parcelado: `scope=plano` cancela o plano inteiro.
   - **NUNCA recrie um lançamento para poder apagá-lo.** Desfazer é apagar o que
     JÁ existe — criar um novo pagamento e excluí-lo não desfaz nada e ainda mexe
     na fatura do mês seguinte (já aconteceu).
   - Exclusão que o usuário não detalhou: chame primeiro com `preview=true`,
     MOSTRE o plano (o que some e o efeito em saldo/fatura) e peça confirmação.
   - O retorno traz uma seção CONFERIDO lida do banco. Se vier `DIVERGÊNCIA`, NÃO
     diga que deu certo — relate o que o banco mostra.
6. **Cadastro** (`fin_manage`, fields em JSON):
   - account: `{"name","kind"(corrente|poupanca|carteira|vale|investimento|outro),"institution","initial_balance","is_default"}`
   - card: `{"name","brand"(visa|mastercard|elo|amex|hipercard|outro),"last4","limit","closing_day","due_day","account"}`
   - category: `{"name","kind"(despesa|receita),"parent"}` — parent agrupa nos insights
   - budget: `{"category"(ou "geral"),"amount","month"(AAAA-MM, omita p/ todo mês)}`
   - recurring: `{"description","amount"(omita se variável),"kind","category","due_day","frequency"(mensal|semanal|anual),"account"/"card","payment_method"}`
   - update/archive: passe `name` do item; renomear = `fields.new_name`.

## Regras
- Valores em reais, formato BR aceito ("1.234,56"). NUNCA invente valor, data ou
  parcela que o usuário não disse — pergunte.
- Registro de gasto simples: execute direto e confirme em UMA frase natural
  (valor, categoria, onde). Criar cadastro novo (conta/cartão/categoria) ou
  excluir: confirme com o usuário antes, a menos que ele já tenha sido explícito.
- Categoria inexistente: a tool lista as parecidas — escolha a melhor; só crie
  nova (`create_category=true`) se o usuário confirmar ou pedir.
- `confirm=true` NÃO é seu: só use depois que a tool RECUSAR (duplicata ou valor
  acima da fatura) E o usuário confirmar em palavras que é isso mesmo. Reenviar
  com `confirm` por conta própria é furar a trava que protege o dinheiro dele.
- Ao cadastrar cartão, pergunte fechamento (`closing_day`) e vencimento
  (`due_day`) — sem eles a fatura assume o mês civil.
- Primeiro uso: categorias padrão + conta "Carteira" são criadas sozinhas; avise.
- Datas do servidor são UTC; o "hoje" das tools já é o fuso do usuário
  (America/Fortaleza). Responda datas como dd/mm.
- Nos insights, conte a história (onde estourou, o que mudou vs mês passado, o
  quanto de fatura futura já está comprometido) — não despeje a tabela crua.
- Schema das tabelas fin_* para SQL avulso (via skill postgres-neo):
  `skill_read(name="financas", file="references/schema.md")`.
