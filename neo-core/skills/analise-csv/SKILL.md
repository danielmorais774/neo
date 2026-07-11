---
name: analise-csv
description: >
  Analisa um CSV (do /workspace ou URL): colunas, tipos, nulos, estatísticas e
  valores mais comuns. Use quando o usuário apontar um CSV para entender/resumir.
keywords: [csv, planilha, dados, coluna, linha, tabela, analise, resumo]
kind: scripted
enabled: true
version: 1
---

# Análise de CSV

## Quando usar
- O usuário deu uma URL de CSV ou pediu para analisar um CSV que já está no
  seu /workspace (baixado antes ou escrito por você).

## Como fazer
1. Se for URL, NÃO baixe manualmente — o script aceita URL direto.
2. Rode o script pronto no SEU sandbox:

```bash
python3 /skills/analise-csv/scripts/analise_csv.py <caminho-ou-url> [--sep ';'] [--top 5]
```

- `<caminho-ou-url>`: caminho relativo ao /workspace (ex.: `dados.csv`) ou URL http(s).
- `--sep`: separador, quando não for vírgula (o script tenta detectar sozinho).
- `--top`: quantos valores mais comuns mostrar por coluna categórica (padrão 3).

3. O script imprime: nº de linhas/colunas, e por coluna: tipo inferido, % de
   nulos, min/média/max (numéricas) ou top valores (categóricas).
4. Responda ao usuário com um RESUMO em linguagem natural do que importa —
   não cole a saída crua inteira.

## Regras
- Arquivo muito grande (> ~50 MB): avise que vai amostrar; o script já corta em
  200k linhas por padrão.
- Se o CSV estiver na MÁQUINA DO USUÁRIO (não no seu /workspace nem numa URL),
  peça para ele enviar/colar, ou leia com as tools de arquivo do desktop —
  o seu sandbox não enxerga o disco dele.
