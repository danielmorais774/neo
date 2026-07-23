# Schema das tabelas de finanças (fin_*)

Para consultas que as tools não cobrem, use `sql_query_ro` (skill postgres-neo).
Dinheiro é SEMPRE `*_cents` (BIGINT, centavos de BRL) — divida por 100.0.
Filtre SEMPRE `deleted_at IS NULL` em `fin_transaction` (soft delete) e
`user_id` correto. Datas `occurred_at`/`invoice_month`/`month` são DATE.

## fin_account — contas
`id, user_id, name, kind (corrente|poupanca|carteira|vale|investimento|outro),
institution, initial_balance_cents, is_default, archived_at, created_at, updated_at`

Saldo = `initial_balance_cents` + Σ transações da conta:
`receita`/`transf_entrada` somam; `despesa`/`transf_saida`/`pagamento_fatura` subtraem.

## fin_card — cartões de crédito
`id, user_id, name, brand, last4, limit_cents, closing_day, due_day,
account_id, archived_at, created_at, updated_at`

Saldo devedor do cartão = Σ `despesa` do cartão − Σ `pagamento_fatura` do cartão.

## fin_category — categorias (2 níveis)
`id, user_id, name, parent_id (NULL = grupo pai), kind (despesa|receita),
is_system, archived_at, created_at`

## fin_transaction — o extrato
`id, user_id, kind (despesa|receita|transf_saida|transf_entrada|pagamento_fatura),
amount_cents (sempre >0; kind dá o sinal), description, merchant, category_id,
account_id, card_id, payment_method (pix|debito|credito|boleto|dinheiro|ted|
transferencia|vale|outro), occurred_at (data do gasto), invoice_month (1º dia do
mês da fatura, só crédito), installment_plan_id, installment_no, transfer_group,
notes, source, created_at, updated_at, deleted_at`

- Compra no crédito: `card_id` set, `account_id` NULL.
- Parcela: `installment_plan_id` + `installment_no`; `occurred_at` fica no mês
  da fatura da parcela (fluxo de caixa), não tudo no dia da compra.
- Gastos do período: `kind='despesa'` (NÃO some `pagamento_fatura` — duplicaria).

## fin_installment_plan — planos de parcelamento
`id, user_id, card_id, description, merchant, category_id, total_cents,
n_installments, purchase_date, first_invoice_month, created_at, canceled_at`

## fin_recurring — assinaturas/contas fixas (cadastro, não lança sozinho)
`id, user_id, description, kind, amount_cents (NULL = variável), category_id,
payment_method, account_id, card_id, due_day, frequency (mensal|semanal|anual),
active, notes, created_at, updated_at`

## fin_budget — orçamentos
`id, user_id, category_id (NULL = geral), month (NULL = todo mês, senão 1º dia
do mês), amount_cents, created_at, updated_at`

## Exemplos
```sql
-- gasto por categoria-mãe no mês
SELECT COALESCE(pc.name, c.name, 'Sem categoria') AS grupo,
       ROUND(SUM(t.amount_cents)/100.0, 2) AS total
FROM fin_transaction t
LEFT JOIN fin_category c ON c.id = t.category_id
LEFT JOIN fin_category pc ON pc.id = c.parent_id
WHERE t.user_id = :uid AND t.deleted_at IS NULL AND t.kind = 'despesa'
  AND t.occurred_at >= date_trunc('month', CURRENT_DATE)
GROUP BY 1 ORDER BY 2 DESC LIMIT 20

-- fatura de um cartão por mês
SELECT invoice_month, ROUND(SUM(amount_cents)/100.0, 2)
FROM fin_transaction
WHERE user_id = :uid AND card_id = :card AND kind = 'despesa'
  AND deleted_at IS NULL
GROUP BY 1 ORDER BY 1 LIMIT 12
```
