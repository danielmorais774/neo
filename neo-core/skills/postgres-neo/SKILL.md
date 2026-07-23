---
name: postgres-neo
description: >
  Consulta o banco Postgres do Neo (PKS, sessões e mensagens de conversa,
  missões, preferências, jobs) com SQL somente-leitura. Use para perguntas
  sobre os dados internos do Neo.
keywords: [banco do neo, banco de dados do neo, postgres do neo, tabela do neo, pks]
kind: tool_backed
register: oficina
tools: [sql_query_ro]
enabled: true
version: 1
---

# Consultar o banco do Neo (Postgres, somente leitura)

## Quando usar
- Perguntas sobre os dados internos do Neo: "quantas conversas tivemos esse
  mês?", "o que tem salvo no meu PKS sobre X?", "quantas missões de WhatsApp
  falharam?", "qual job demorou mais?".
- NÃO use para conhecimento pessoal comum — para isso o `search_pks` (busca
  semântica) é melhor. Use SQL quando precisar de CONTAGEM, agregação, filtro
  por data/status ou junção entre tabelas.

## Como usar
1. Chame `sql_query_ro` com UM statement SELECT (ou WITH). Exemplos:

```sql
SELECT count(*) FROM conversation_session WHERE started_at >= now() - interval '30 days'
SELECT status, count(*) FROM mission GROUP BY status ORDER BY 2 DESC
SELECT name, type FROM pks_entity WHERE deleted_at IS NULL AND type = 'device' LIMIT 20
```

2. Schema completo das tabelas: leia
   `skill_read(name="postgres-neo", file="references/schema.md")` — consulte
   ANTES de escrever queries com joins ou colunas que você não tem certeza.

## Regras
- SEMPRE use LIMIT (a tool corta em 200 linhas de qualquer jeito) e prefira
  agregações (count/avg/group by) a despejar linhas cruas.
- Escrita é IMPOSSÍVEL (sessão read-only) — não tente INSERT/UPDATE/DELETE.
- Timestamps são UTC; converta mentalmente para o fuso do usuário ao responder.
- PKS: filtre `deleted_at IS NULL` (soft delete) ou vai contar entidade morta.
- Responda em linguagem natural com os números — não cole a tabela crua, a
  menos que o usuário peça os dados.
