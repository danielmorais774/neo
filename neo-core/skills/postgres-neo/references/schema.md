# Schema do neodb (tabelas principais)

Colunas listadas são as úteis para consulta; timestamps em UTC.

## Conversas

### conversation_session
- `id` uuid PK · `user_id` uuid · `service` varchar (ex. neo-desktop) ·
  `status` varchar (active/…) · `topic_summary` text · `message_count` int ·
  `started_at` / `last_activity_at` / `ended_at` timestamps · `session_metadata` json

### conversation_message
- `id` uuid PK · `session_id` uuid FK→conversation_session (CASCADE) ·
  `user_id` uuid · `mission_id` uuid FK→mission · `role` varchar (user/assistant/…) ·
  `content` text · `tool_name` varchar · `token_count` int · `created_at`

## PKS (conhecimento pessoal) — soft delete: filtre `deleted_at IS NULL`

### pks_entity
- `id` uuid PK · `workspace_id` uuid · `space_id` / `collection_id` uuid ·
  `name` text · `type` varchar (device, person, place, rule, …) · `aliases` text[] ·
  `metadata` jsonb · `confidence` numeric · `sensitivity` varchar ·
  `source` varchar · `last_used_at` · `created_at` / `updated_at` / `deleted_at`

### pks_entity_field
- `id` uuid PK · `entity_id` uuid FK→pks_entity · `key` text ·
  `value_type` varchar · `value_text` text · `value_number` numeric ·
  `value_date` timestamp · `value_json` jsonb (regras de comportamento vivem aqui) ·
  `unit` text · `confidence` · `created_at` / `updated_at` / `deleted_at`

### pks_entity_relation
- liga duas entidades: `from_entity_id` / `to_entity_id` / `relation_type`
  (ex. located_in) · `deleted_at`

### pks_workspace / pks_space / pks_collection
- hierarquia de organização; `pks_workspace.owner_user_id` liga ao usuário.

## Ações / automação

### mission (missões de WhatsApp: pedidos, recados)
- `id` uuid PK · `user_id` · `session_id` · `title` varchar · `goal` text ·
  `contact_jid` varchar · `channel` varchar (whatsapp) ·
  `status` varchar (pending/awaiting_confirmation/awaiting_reply/processing/completed/cancelled/failed) ·
  `next_action_type` varchar · `created_at` / `updated_at`

### agent_job (jobs em background no sandbox)
- `id` uuid PK · `user_id` · `session_id` · `kind` varchar · `command` text ·
  `status` varchar (queued/running/succeeded/failed/…) · `exit_code` int ·
  `created_at` / `started_at` / `finished_at`

## Diversos

### user_preference
- `user_id` · `service` varchar · `category` varchar · `key` varchar ·
  `value` text (json em string p/ ex. voz ativa: service=neo-voice, category=tts, key=active)

### connected_device
- dispositivos que já conectaram: `device_id`, `hardware_id`, `os_platform`,
  `last_seen_at` etc.

### whatsapp_contact
- agenda resolvida: `jid`, `label`/nome, telefone.

### user_notification
- notificações proativas enviadas ao usuário.

### user_message
- mensagens avulsas por canal (ex. WhatsApp inbound fora de sessão).

## Finanças pessoais (fin_*)

Tabelas `fin_account`, `fin_card`, `fin_category`, `fin_transaction`,
`fin_installment_plan`, `fin_recurring`, `fin_budget` — extrato, cartões,
parcelas, orçamentos. Dinheiro em `*_cents` (centavos BRL); `fin_transaction`
tem soft delete (`deleted_at IS NULL`). Schema completo + exemplos:
`skill_read(name="financas", file="references/schema.md")`.
