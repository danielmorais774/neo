Inspeciona as entidades do workspace pessoal de um usuário no neodb.

Sem argumento lista todos os workspaces disponíveis. Com um nome ou UUID de workspace mostra as entidades desse usuário com timestamps e resumo de props.

**Uso:**
```
# listar workspaces
DOCKER_HOST=ssh://neo-vagrant docker exec neo-core \
  python /app/.claude/commands/scripts/inspect_user.py --list

# inspecionar workspace por nome ou UUID
DOCKER_HOST=ssh://neo-vagrant docker exec neo-core \
  python /app/.claude/commands/scripts/inspect_user.py "<workspace-name-or-uuid>" [--type <type>] [--entity <name>] [--since <duration>] [--limit N]
```

Execute esse comando via Bash tool com DOCKER_HOST=ssh://neo-vagrant. O script é read-only, nunca escreve no banco.

$ARGUMENTS
