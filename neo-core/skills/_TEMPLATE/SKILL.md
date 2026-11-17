---
# Pastas começando com "_" são IGNORADAS pelo loader — este é só o modelo.
name: nome-da-skill              # ^[a-z0-9][a-z0-9-]{1,39}$ — igual ao nome da pasta
description: >                   # ≤ 200 chars. É a ÚNICA coisa que o cérebro vê
  Uma frase dizendo O QUE a skill faz e QUANDO usar. Capriche: é ela que decide
  se o modelo lê o resto.
keywords: [palavra, chave, singular]   # prefiltro do catálogo quando ele cresce
kind: instructional              # instructional | scripted | tool_backed
# register: oficina              # opcional: registro de tom imposto enquanto a skill
                                 # está ativa (silencioso|parceiro|oficina|combate|
                                 # protocolo|confidente); omita para não impor
tools: []                        # só tool_backed: tools destravadas pela leitura
env: []                          # só scripted: envs que os scripts esperam no sandbox
requires: []                     # gates de runtime ("workbench" é implícito p/ scripted)
enabled: true
version: 1
---

# Título da skill

## Quando usar
- Situações concretas em que esta skill se aplica.

## Como fazer
1. Passos imperativos e curtos. Diga QUAL tool chamar e com QUAIS argumentos.
2. Para scripts (kind=scripted): use o caminho do sandbox, ex.
   `python3 /skills/nome-da-skill/scripts/exemplo.py entrada.csv`.
3. Detalhe grande (schemas, tabelas, exemplos longos) vai em `references/` e é
   lido sob demanda: `skill_read(name="nome-da-skill", file="references/x.md")`.

## Regras
- O corpo inteiro é pago em tokens quando lido — alvo: < 1.500 tokens.
- Não duplique o que o cérebro já sabe (persona, regras gerais do Neo).
- Lembre: o sandbox NÃO alcança a LAN/containers — skill que precisa de recurso
  interno (Postgres, HA) é tool_backed, não scripted.
