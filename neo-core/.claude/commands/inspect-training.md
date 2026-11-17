Health-check das 5 entidades de treino do Neo no neodb.

Varre `neo-personality`, `neo-response-rules`, `neo-operating-procedures`, `neo-training-examples` e `neo-communication-preferences`. Para cada regra/procedimento/exemplo lista `rule_key`, `priority` e `description`. Sinaliza automaticamente:

- `envelope_in_description` — texto de envelope ("modo treino", "salve este aprendizado" etc.) vazou na description de uma regra ativa  
- `hash_like_rule_key` — rule_key tem sufixo hash (`_xxxxxxxx`) em vez de ser semântico  
- `raw_copied_as_description` — texto bruto foi copiado direto como description  
- `persona_slightly_funny` — trait `slightly_funny` misturado com traits ácidos (regressão de persona)  

Exit code 0 = tudo limpo. Exit code 1 = há problemas.

**Uso:**
```
DOCKER_HOST=ssh://neo-vagrant docker exec neo-core \
  python /app/.claude/commands/scripts/inspect_training.py [--json]
```

Com `--json` retorna JSON estruturado em vez do relatório human-readable.

Execute esse comando via Bash tool com DOCKER_HOST=ssh://neo-vagrant. O script é read-only, nunca escreve no banco.

$ARGUMENTS
