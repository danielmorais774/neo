#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$ROOT_DIR/neo-core"

TEST_MESSAGE="${TEST_MESSAGE:-Oi, meu nome é Daniel Morais}"
TEST_PROVIDER="${TEST_PROVIDER:-deepseek}"
TEST_MODEL="${TEST_MODEL:-deepseek-chat}"
TEST_USER_EMAIL="${TEST_USER_EMAIL:-}"
TEST_USER_ID="${TEST_USER_ID:-}"
TEST_TIMEOUT_SECONDS="${TEST_TIMEOUT_SECONDS:-120}"

MAIN_PG_SERVICE="neo-postgres"
TEST_PG_SERVICE="neo-postgres-pks-test"
TEST_CORE_SERVICE="neo-core-pks-test"
TEST_DB_NAME="neodb"

usage() {
  cat <<'EOF'
Uso:
  ./scripts/pks_first_message_test.sh up
  ./scripts/pks_first_message_test.sh run [--email EMAIL | --user-id UUID] [--message "Oi, meu nome é Daniel Morais"] [--provider deepseek] [--model deepseek-chat]
  ./scripts/pks_first_message_test.sh down

Variaveis opcionais:
  TEST_USER_EMAIL
  TEST_USER_ID
  TEST_MESSAGE
  TEST_PROVIDER
  TEST_MODEL
  TEST_TIMEOUT_SECONDS
EOF
}

log() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Comando obrigatorio nao encontrado: $1" >&2
    exit 1
  }
}

compose() {
  docker compose --profile pks-test "$@"
}

wait_for_postgres() {
  local service="$1"
  local retries=30
  local i
  for ((i=1; i<=retries; i++)); do
    if compose exec -T "$service" pg_isready -U neo -d "$TEST_DB_NAME" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Postgres do service $service nao ficou pronto a tempo" >&2
  exit 1
}

wait_for_api() {
  local retries=60
  local i
  for ((i=1; i<=retries; i++)); do
    if compose exec -T "$TEST_CORE_SERVICE" python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).read()
PY
    then
      return 0
    fi
    sleep 2
  done
  echo "API do service $TEST_CORE_SERVICE nao ficou pronta a tempo" >&2
  exit 1
}

restart_test_api() {
  log "Reiniciando API de teste para limpar caches apos reset/migrate"
  compose restart "$TEST_CORE_SERVICE"
  wait_for_api
}

up() {
  log "Subindo neo-postgres-pks-test e neo-core-pks-test"
  compose up -d neo-mongo "$TEST_PG_SERVICE" "$TEST_CORE_SERVICE"
  wait_for_postgres "$TEST_PG_SERVICE"
  wait_for_api
  log "Ambiente de teste pronto em http://127.0.0.1:8001"
}

reset_test_database() {
  log "Resetando schema do banco de teste"
  compose exec -T "$TEST_PG_SERVICE" psql -U neo -d "$TEST_DB_NAME" -v ON_ERROR_STOP=1 <<'SQL'
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
}

run_migrations() {
  log "Rodando migrations no banco de teste"
  compose exec -T "$TEST_CORE_SERVICE" alembic upgrade head
}

replicate_users() {
  log "Replicando tabela user do banco principal para o banco de teste"
  docker compose exec -T "$MAIN_PG_SERVICE" pg_dump \
    -U neo \
    --data-only \
    --column-inserts \
    -t 'public."user"' \
    neodb | compose exec -T "$TEST_PG_SERVICE" psql -U neo -d "$TEST_DB_NAME" -v ON_ERROR_STOP=1
}

resolve_test_user() {
  local sql=""

  if [[ -n "$TEST_USER_ID" ]]; then
    sql="SELECT id::text, COALESCE(display_name, username, email), COALESCE(email, '') FROM \"user\" WHERE id = '$TEST_USER_ID' LIMIT 1;"
  elif [[ -n "$TEST_USER_EMAIL" ]]; then
    sql="SELECT id::text, COALESCE(display_name, username, email), COALESCE(email, '') FROM \"user\" WHERE lower(email) = lower('$TEST_USER_EMAIL') LIMIT 1;"
  else
    sql="SELECT id::text, COALESCE(NULLIF(display_name, ''), NULLIF(username, ''), COALESCE(email, '')), COALESCE(email, '') FROM \"user\" ORDER BY created_at ASC LIMIT 1;"
  fi

  local row
  row="$(compose exec -T "$TEST_PG_SERVICE" psql -U neo -d "$TEST_DB_NAME" -At -F $'\t' -c "$sql")"
  if [[ -z "$row" ]]; then
    echo "Nenhum usuario encontrado no banco de teste. Passe TEST_USER_EMAIL ou TEST_USER_ID." >&2
    exit 1
  fi

  IFS=$'\t' read -r TEST_USER_ID RESOLVED_USER_NAME RESOLVED_USER_EMAIL <<<"$row"
  export TEST_USER_ID RESOLVED_USER_NAME RESOLVED_USER_EMAIL
  log "Usuario de teste: ${RESOLVED_USER_NAME} (${TEST_USER_ID})"
}

post_first_message() {
  log "Enviando primeira mensagem via /api/v1/desktop/chat com provider=$TEST_PROVIDER model=$TEST_MODEL"
  compose exec -T "$TEST_CORE_SERVICE" env \
    TEST_USER_ID="$TEST_USER_ID" \
    TEST_MESSAGE="$TEST_MESSAGE" \
    TEST_PROVIDER="$TEST_PROVIDER" \
    TEST_MODEL="$TEST_MODEL" \
    TEST_TIMEOUT_SECONDS="$TEST_TIMEOUT_SECONDS" \
    python - <<'PY'
import json
import os
import urllib.request

payload = {
    "type": "user_input",
    "user_id": os.environ["TEST_USER_ID"],
    "user_message": os.environ["TEST_MESSAGE"],
    "provider": os.environ["TEST_PROVIDER"],
    "model": os.environ["TEST_MODEL"],
}

req = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/desktop/chat",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
timeout = int(os.environ.get("TEST_TIMEOUT_SECONDS", "120"))
with urllib.request.urlopen(req, timeout=timeout) as resp:
    body = resp.read().decode("utf-8")
print(body)
PY
}

print_pks_tables() {
  log "Dump das tabelas pks_* no banco de teste"
  compose exec -T "$TEST_PG_SERVICE" psql -U neo -d "$TEST_DB_NAME" -c '\dt pks_*'
  for table in \
    pks_workspace \
    pks_space \
    pks_collection \
    pks_entity \
    pks_entity_field \
    pks_entity_relation \
    pks_audit_log \
    pks_workspace_link
  do
    log "SELECT * FROM $table"
    compose exec -T "$TEST_PG_SERVICE" psql -U neo -d "$TEST_DB_NAME" -c "SELECT * FROM $table;"
  done
}

assert_expected_state() {
  log "Validando estado esperado do PKS"
  compose exec -T "$TEST_PG_SERVICE" psql -U neo -d "$TEST_DB_NAME" -v ON_ERROR_STOP=1 <<SQL
\set ON_ERROR_STOP on

DO \$\$
DECLARE
  v_workspace_name text;
  v_workspace_count int;
  v_space_count int;
  v_primary_count int;
  v_duplicate_count int;
  v_full_name text;
BEGIN
  SELECT count(*), max(name)
    INTO v_workspace_count, v_workspace_name
  FROM pks_workspace
  WHERE owner_user_id = '$TEST_USER_ID'::uuid
    AND deleted_at IS NULL;

  IF v_workspace_count <> 1 THEN
    RAISE EXCEPTION 'Esperava 1 workspace ativo, encontrei %', v_workspace_count;
  END IF;

  IF v_workspace_name <> 'Daniel Morais' THEN
    RAISE EXCEPTION 'Workspace deveria ser Daniel Morais, veio %', v_workspace_name;
  END IF;

  SELECT count(*)
    INTO v_space_count
  FROM pks_space s
  JOIN pks_workspace w ON w.id = s.workspace_id
  WHERE w.owner_user_id = '$TEST_USER_ID'::uuid
    AND w.deleted_at IS NULL
    AND s.deleted_at IS NULL;

  IF v_space_count <> 7 THEN
    RAISE EXCEPTION 'Esperava 7 spaces ativos, encontrei %', v_space_count;
  END IF;

  SELECT count(*)
    INTO v_primary_count
  FROM pks_entity e
  JOIN pks_space s ON s.id = e.space_id
  JOIN pks_workspace w ON w.id = e.workspace_id
  WHERE w.owner_user_id = '$TEST_USER_ID'::uuid
    AND e.deleted_at IS NULL
    AND s.deleted_at IS NULL
    AND s.name = 'Perfil'
    AND COALESCE((e.metadata->>'is_primary_user')::boolean, false) = true
    AND e.name = 'Daniel Morais';

  IF v_primary_count <> 1 THEN
    RAISE EXCEPTION 'Esperava 1 entity primaria Daniel Morais em Perfil, encontrei %', v_primary_count;
  END IF;

  SELECT count(*)
    INTO v_duplicate_count
  FROM pks_entity e
  JOIN pks_space s ON s.id = e.space_id
  JOIN pks_workspace w ON w.id = e.workspace_id
  WHERE w.owner_user_id = '$TEST_USER_ID'::uuid
    AND e.deleted_at IS NULL
    AND s.deleted_at IS NULL
    AND s.name = 'Pessoas'
    AND e.name = 'Daniel Morais';

  IF v_duplicate_count <> 0 THEN
    RAISE EXCEPTION 'Nao deveria haver duplicata Daniel Morais em Pessoas, encontrei %', v_duplicate_count;
  END IF;

  SELECT ef.value_text
    INTO v_full_name
  FROM pks_entity_field ef
  JOIN pks_entity e ON e.id = ef.entity_id
  JOIN pks_workspace w ON w.id = e.workspace_id
  WHERE w.owner_user_id = '$TEST_USER_ID'::uuid
    AND ef.deleted_at IS NULL
    AND e.deleted_at IS NULL
    AND ef.key = 'full_name'
  LIMIT 1;

  IF v_full_name <> 'Daniel Morais' THEN
    RAISE EXCEPTION 'Campo full_name deveria ser Daniel Morais, veio %', v_full_name;
  END IF;
END \$\$;
SQL

  log "Validacao concluida"
}

down() {
  log "Derrubando ambiente pks-test e removendo volumes"
  compose down -v --remove-orphans
}

run() {
  up
  reset_test_database
  run_migrations
  replicate_users
  restart_test_api
  resolve_test_user
  post_first_message
  print_pks_tables
  assert_expected_state
}

main() {
  require_cmd docker

  local command="${1:-run}"
  shift || true

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --email)
        TEST_USER_EMAIL="$2"
        shift 2
        ;;
      --user-id)
        TEST_USER_ID="$2"
        shift 2
        ;;
      --message)
        TEST_MESSAGE="$2"
        shift 2
        ;;
      --provider)
        TEST_PROVIDER="$2"
        shift 2
        ;;
      --model)
        TEST_MODEL="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Argumento desconhecido: $1" >&2
        usage
        exit 1
        ;;
    esac
  done

  case "$command" in
    up) up ;;
    run) run ;;
    down) down ;;
    *)
      echo "Comando desconhecido: $command" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
