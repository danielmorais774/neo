Inspeciona as entidades do workspace de sistema do Neo no neodb.

Lê e exibe: cada entidade com `name [type]`, UUID, timestamps `created`/`updated`, resumo das props (listas como `list[N]`, escalares inline). Com `--entity <nome>` despeja o JSON completo das propriedades dessa entidade. Com `--type <tipo>` filtra por tipo de entidade. Com `--since <duração>` (ex: `24h`, `30m`, `2d`) mostra só entidades atualizadas nessa janela.

**Uso:**
```
DOCKER_HOST=ssh://neo-vagrant docker exec neo-core \
  python /app/.claude/commands/scripts/inspect_neo.py [--entity <name>] [--type <type>] [--since <duration>] [--limit N]
```

Execute esse comando via Bash tool com DOCKER_HOST=ssh://neo-vagrant. O script é read-only, nunca escreve no banco.

$ARGUMENTS
