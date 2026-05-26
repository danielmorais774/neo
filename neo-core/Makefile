# Makefile — Neo Core
# Código é copiado no build da imagem, então mudanças em
# migrations/models precisam de rebuild antes de executar.

.PHONY: help build migrate migrate-create migrate-history migrate-current migrate-downgrade db-shell db-tables pks-test-up pks-test-run pks-test-down

help: ## Mostra comandos disponíveis
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Build ────────────────────────────────────────────────

build: ## Rebuild e restart do neo-core
	docker compose build neo-core && docker compose up neo-core -d

# ── Migrations ───────────────────────────────────────────

migrate: build ## Rebuild + roda migrations pendentes
	docker compose exec neo-core alembic upgrade head

migrate-create: ## Cria migration local (uso: make migrate-create name="descricao")
	cd /home/dev/projects/neo/neo-core && uv run alembic revision -m "$(name)"

migrate-history: ## Mostra histórico de migrations no banco
	docker compose exec neo-core alembic history --verbose

migrate-current: ## Mostra migration atual aplicada no banco
	docker compose exec neo-core alembic current

migrate-downgrade: build ## Rebuild + desfaz última migration
	docker compose exec neo-core alembic downgrade -1

# ── Seeds ─────────────────────────────────────────────────

seed: build ## Rebuild + roda seeds (dados iniciais)
	docker compose exec neo-core python -m db.seeds

# ── Database ─────────────────────────────────────────────

db-shell: ## Abre psql no banco neodb
	docker compose exec neo-postgres psql -U neo -d neodb

db-tables: ## Lista tabelas do banco
	docker compose exec neo-postgres psql -U neo -d neodb -c "\dt"

# ── PKS test harness ─────────────────────────────────────

pks-test-up: ## Sobe ambiente isolado de teste do PKS
	./scripts/pks_first_message_test.sh up

pks-test-run: ## Reseta banco de teste, replica users, envia a 1a mensagem e confere pks_*
	./scripts/pks_first_message_test.sh run

pks-test-down: ## Derruba ambiente isolado de teste do PKS e remove volume
	./scripts/pks_first_message_test.sh down
