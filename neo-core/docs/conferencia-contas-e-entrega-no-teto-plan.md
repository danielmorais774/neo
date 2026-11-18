# Conferência de contas e entrega no teto de rodadas — plano

> Status (17/11/2026): F1–F4 EM PRODUÇÃO (commit no branch
> `conferencia-contas-teto`); falta o E2E (§5). Origem: session-3b19d27a ("passa
> OCR na notinha do e-mail, põe numa planilha e me devolve o arquivo"). O que
> saiu diferente do plano está em §6.

## 1. O que aconteceu

| Turno | Tempo | Rodadas | Resultado |
|---|---|---|---|
| 1 — o pedido | 156 s | 33 (teto 32 + a final sem tools) | nenhuma planilha; "me dá um 'vai'" |
| 2 — "gera ai" | 33 s | ~6 | planilha entregue, com um valor errado |

Dois defeitos independentes:

1. **Sem entrega no turno 1.** A 1ª leitura completa da nota saiu na 10ª rodada
   (os 8 itens, o total de 213,74 e as formas de pagamento). As ~22 rodadas
   seguintes foram releituras em faixas, todas com a mesma pergunta: "quantas
   linhas aparecem, alguma está cortada?". O modelo queria ter certeza de que não
   faltava linha, e nada no turno conseguia dar essa certeza. Ao bater o teto, o
   `_AVISO_FIM_DAS_RODADAS` pediu "o que ficou pronto, o que faltou e onde
   parou". Ele respondeu com uma oferta ("me dá um vai"). O verificador de
   promessa leu como pergunta ao usuário (`promised=false`) e não continuou. O
   "gera ai" foi uma autocontinuação feita à mão.
2. **Conta errada, afirmada como certa.** A visão leu a vodka como 1 × 39,99 =
   33,99. Os itens só fecham com o total de 213,74 se a vodka for **39,99**: com
   33,99 a soma dá 207,74. O modelo escreveu o inverso ("só fecha se a vodka for
   33,99"), gravou 33,99 na planilha (`TOTAL = SUM(F6:F13)` = 207,74, ao lado de
   "Total pago" = 213,74) e disse "Total fecha em R$ 213,74".

Achados menores: o aviso `midia=tela` mandou ler a skill `ocr-video` numa
**foto** (1 rodada, mais 2 rodadas de tesseract com resultado ilegível). A 1ª
`email_inbox` não achou a mensagem, que chegou no mesmo minuto do pedido (1
rodada; corrida, não bug).

## 2. Princípios

- **A conta fica no código** ([[feedback-decisao-em-codigo-nao-no-llm]]). O modelo
  errou justamente a aritmética. Ele recebe o veredito pronto, não uma instrução
  "confira as contas".
- **Reaproveitar o que já existe**: `[[HANDOFF:continuar]]` + nota de trabalho do
  `tool_trace` (self-continuation), `annotate_result` do loop, e a
  `core/files/conferencia.py`, que já confere arquivo publicado.
- **Nada muda em turno que não bate no teto nem publica planilha.**
- Constante de comportamento nova vai para `config/neo.toml`, via
  `shared/ajustes.py` ([[feedback-constantes-no-neo-toml]]).

## 3. Fases

Ordem pensada para F1 dar o ganho visível sozinha. F2 a F4 se apoiam umas nas
outras, mas cada uma vai para produção separada.

### F1 — Teto de rodadas vira continuação automática

**Hoje:** no teto, a rodada final sem tools sai com o aviso "responda o que
faltou", e o turno acaba ali.

**Mudança:**

1. `_LoopRun` ([ai_service.py:3596](../modules/desktop/ai_service.py)) ganha
   `parou_no_teto: bool`. O loop ([ai_service.py:8303](../modules/desktop/ai_service.py))
   marca o campo quando a rodada final aconteceu por `round_idx >= max_rounds`. O
   limite de repetição (`encerrar_por_repeticao`) **não** marca: ali o modelo está
   travado, e continuar repetiria o travamento.
2. O loop recebe `continuar_no_teto: bool`, que o chamador calcula. Com ele
   ligado, o aviso final troca para uma variante nova,
   `_AVISO_FIM_DAS_RODADAS_CONTINUA`: "As rodadas deste passo acabaram e o
   trabalho segue sozinho no próximo. Diga em uma frase o que já levantou e o
   que falta. Não pergunte nada ao usuário." Isso corta o "me dá um vai".
3. No bloco da autocontinuação ([ai_service.py:6960](../modules/desktop/ai_service.py)),
   antes de chamar `_verify_promise_continuation`, o código cria a continuação
   **sem LLM** quando tudo isto vale junto:
   - `_run.parou_no_teto`;
   - nenhum `file_save` no turno (`produced_files` vazio);
   - nenhum job aberto (`_open_jobs(tool_trace)` vazio). Com job aberto, quem
     continua é o `then`;
   - o passo atual **não é** uma continuação de teto (ver o item 4);
   - `pipeline_self_handoff_enabled` e `ajustes().cerebro.continuar_no_teto`.

   A ordem: "Termine o pedido original: «{user_message}». O que já foi feito está
   na nota de trabalho; não refaça e não releia o que já foi lido. Entregue agora
   com o que tem e aponte na resposta o que ficou incerto." A nota de trabalho
   continua saindo do `tool_trace`, como hoje.
4. **Uma continuação de teto por pedido.** O `Handoff` sintetizado leva uma marca
   (`context_note` ou um campo `motivo="teto"`). Se o passo de continuação também
   bater o teto, não nasce outra: ele termina com o aviso antigo. O orçamento do
   orquestrador (`pipeline_self_handoff_max_steps = 8`) continua de rede.
5. `[cerebro] continuar_no_teto = true` em `shared/ajustes.py` e `config/neo.toml`.

**Testes:** estender `tests/test_cognitive_fim_das_rodadas.py`:
- teto sem `file_save` → handoff `is_self` com a ordem e a marca de teto;
- teto com `file_save` → nada;
- parada por repetição → nada;
- teto num passo de continuação de teto → nada;
- flag desligada → comportamento de hoje, com o aviso antigo;
- com `continuar_no_teto`, o aviso final é a variante nova.

**Risco:** um teto que hoje acaba em "não consegui" passa a gastar mais um passo
de até 32 rodadas. O limite de uma continuação por pedido segura o custo. Na
sessão, esse passo levou 33 s.

### F2 — Função de conferência de contas (pura)

`core/files/contas.py`, só biblioteca padrão e `Decimal`, sem I/O:

```python
conferir(itens: list[Item], totais: Totais) -> Veredito
# Item:   codigo, descricao, qtd, unidade, unitario, total
# Totais: valor_a_pagar, qtde_itens, pagamentos[], troco, descontos[]
```

1. **Linha:** `qtd × unitário` comparado com o `total` da linha. Tolerância de
   0,01 (item por peso arredonda: 2,168 × 23,99 = 52,01). Marca as linhas que
   não batem e a diferença de cada uma.
2. **Fechamento:**
   - soma dos totais − descontos = valor a pagar;
   - soma dos pagamentos − troco = valor a pagar;
   - número de linhas = qtde total de itens.
3. **Dedução** (só se o fechamento não bate, com diferença D):
   - uma linha marcada no passo 1 com diferença igual a D → **duas provas
     independentes** apontam a mesma leitura errada → confiança alta, com a
     correção (total = qtd × unitário);
   - senão, troca de um dígito em cada valor lido, com as confusões comuns de OCR
     (3↔8↔9, 0↔8, 1↔7, 5↔6, 4↔9). Lista as trocas que zeram D. Uma troca só:
     confiança média. Várias: lista, para reler só essas linhas;
   - nada explica: "não fecha por D; falta linha ou o total foi mal lido".
4. **Cobertura:** fechamento ok + contagem ok = "todas as linhas foram lidas".
   Era a certeza que o modelo buscou por 22 rodadas.

O `Veredito` sai em texto curto, pronto para o modelo ler. No caso da sessão:
"Vodka: 1 × 39,99 ≠ 33,99 (dif. 6,00). Itens somam 207,74, a pagar 213,74 (dif.
6,00). A mesma diferença nas duas: o total da vodka é 39,99. Com isso fecha, e as
8 linhas batem com 'Qtde total de itens: 8'."

**Testes:** `tests/files/test_contas.py`:
- a nota da sessão inteira, como fixture: leitura errada → dedução 39,99;
  leitura certa → fecha e cobre;
- a leitura de 3,99 no gelo (que também apareceu numa das passadas) → troca de
  dígito 3↔5;
- item por peso, desconto, troco, linha faltando, duas linhas erradas (não deduz
  com confiança alta).

### F3 — Rede de segurança: a planilha publicada é conferida

No `file_save` de `.xlsx` (e `.csv`), uma checagem determinística:

1. Abre com openpyxl, que **não calcula fórmula**, e avalia ela mesma as
   `=SUM(X:Y)` de intervalo simples. Fórmula complexa é pulada, sem acusar.
2. Procura a linha com rótulo de total ("total", "a pagar", "total pago",
   "soma") e compara os totais entre si e com a soma calculada.
3. Tabela com colunas reconhecíveis de qtd / unitário / total → aplica o passo 1
   da F2 linha a linha.
4. Achou divergência → o aviso entra **colado ao resultado do `file_save`**, pelo
   mesmo canal do `annotate_result` (hoje só tem o `_origin_annotator`; passa a
   compor dois). Exemplo: "Conferência da planilha: TOTAL (F14) = 207,74, Total
   pago (B19) = 213,74, diferença 6,00. Linha 12: 1 × 39,99 ≠ 33,99. Corrija e
   salve de novo antes de responder." O modelo ainda tem rodadas, então corrige.
5. A resposta final que cita um total diferente do calculado ("fecha em
   213,74") entra na guarda de valor afirmado que já existe
   (`_valores_ausentes_no_arquivo`). Cuidado: hoje ela só pega valor em
   negrito/código, e um valor monetário perto de "total" precisaria entrar no
   `valores_afirmados` sem gerar ruído. Medir no corpus antes
   ([[neo-guard-heuristics-measure-on-prod-corpus]]).

Isso vale para qualquer planilha que o Neo gera, não só nota fiscal.

**Testes:** xlsx gerado no teste com a planilha da sessão → aviso com 207,74 ×
213,74 e linha 12. Planilha que fecha → nada. Fórmula complexa → nada.

### F4 — Conferência durante a leitura (antes da planilha)

1. **Tool `conferir_cupom`**, server-side, executada pela F2. Fica atrás de uma
   skill `nota-fiscal` do tipo `tool_backed`, então o schema só entra no turno
   depois do `skill_read` (tool gating, que já existe). A skill diz: ler a imagem
   inteira com `file_olhar` uma vez, mandar os itens e totais para
   `conferir_cupom`, reler **só** as linhas que o veredito apontar, e gerar a
   planilha a partir dos valores conferidos.
2. **Rota de imagem ≠ vídeo.** Quando `email_anexo`, `file_stage` ou
   `request_user_file` devolvem `mime: image/*` num turno com `midia=tela`, o
   resultado ganha a anotação: "é imagem, não vídeo: leia com `file_olhar`
   inteira; se for nota ou cupom, leia a skill `nota-fiscal`; a `ocr-video` não
   serve". Decisão no código, pelo `mime`. O classificador não sabe o tipo, porque
   o anexo ainda estava no e-mail.
3. Com a F2 respondendo "fecha e cobre as 8 linhas" na 10ª rodada, as
   releituras deixam de ter motivo. Isso deve cortar a maior parte das 22
   rodadas sem limitar o `file_olhar`.

### Fora do escopo (melhoria futura)

- **Leitura fatiada.** O Daniel decidiu deixar para depois: não prejudicou o
  resultado, só o tempo. Se a F4 não resolver, a opção é um teto de
  `file_olhar` sobre recortes do mesmo arquivo por turno.
- **Chave de acesso NFC-e.** São 44 dígitos com DV módulo 11 (valida a leitura da
  chave), e o QR code leva à consulta da SEFAZ com os itens exatos, sem OCR.
  Depende de a SEFAZ-CE aceitar o IP da VPS ([[neo-browser-servidor-bloqueado-por-origem]]).
- **Prompt do `file_olhar`.** O `_OLHAR_SISTEMA` é de conferência de documento
  (topo, campos, problemas) e foi usado para transcrever. Funcionou, mas um modo
  "transcrever" com `max_tokens` maior que 900 ajudaria notas longas.
- **Limpeza do sandbox.** A foto e ~12 recortes da sessão continuam em
  `/workspace/files` ([[feedback-no-conversation-copies-on-vps]]).

## 4. Como publicar

Mudança espalhada no `ai_service.py`, que é quente (salvar = deploy). Usar a
cópia no scratchpad ([[neo-dev-em-copia-no-scratchpad]]): rsync para `dev/` e
`base/`, testes na cópia, `cmp` de 3 vias e publicação de uma vez. O
`shared/ajustes.py` vai **antes** do `config/neo.toml`.

## 5. Teste ponta a ponta

Pré-requisito: a mesma nota no e-mail do Neo (a mensagem 145 já está lá).

```
te encaminhei um email com a notinha de supermercado quero que passe um OCR nela, coloque em uma planilha e me retorne o arquivo para eu baixar.
```

| Depois de | Esperado |
|---|---|
| F1 | um turno só; se bater o teto, aparece "Continuação" no log e o cartão de download chega sem "gera ai" |
| F3 | se a planilha sair com 33,99, o `file_save` volta com o aviso e a versão final tem a vodka a 39,99 e TOTAL 213,74 |
| F4 | `skill_read nota-fiscal` (não `ocr-video`), um `conferir_cupom` dizendo que fecha, bem menos de 32 rodadas |

O teste não mede a leitura fatiada nem a rota de e-mail (corrida da 1ª
`email_inbox`). Conferir pelo id da sessão no `session-<id>.md`.

## 6. O que foi implementado (17/11) e o que mudou do plano

- **F1** — `_continuacao_do_teto` + `_AVISO_FIM_DAS_RODADAS_CONTINUA` em
  `modules/desktop/ai_service.py`; `[cerebro] continuar_no_teto`. A marca de "uma
  por pedido" ficou mais forte: **só o passo do pedido** (sem
  `handoff_origin_message` na metadata) cria a continuação; nenhum passo de handoff
  cria outra. A ordem NÃO repete o pedido original: o `cycle_history` já o leva, e
  copiar faria a `order_is_near_repeat` ler a ordem como repetição.
  **Mudança no aviso:** a rodada final pede os DADOS levantados, compactos (até
  1.000 caracteres), e não "uma frase". A transcrição do `file_olhar` não entra na
  nota de trabalho (feita do `tool_trace`) nem no caderno; só sobrevive no texto
  desta rodada, que o próximo passo recebe cortado em `_CYCLE_REPLY_CHARS` (1200).
- **F2** — `core/files/contas.py` (`conferir`, `de_dicionarios`).
- **F3** — `conferir_planilha` no mesmo módulo. **Sem openpyxl:** ele não está
  no core (nem no venv nem na imagem), então o .xlsx é lido pelo XML com a
  biblioteca padrão, e o próprio módulo calcula `SUM` e `+ − × ÷` entre células.
  Fica no `_anotador_do_turno`, que junta o aviso de origem que já existia com a
  conferência; `[cerebro] conferir_planilhas`. Só confere aba com tabela de itens
  (colunas de quantidade, unitário e total). **Não feito:** o item 5 (resposta
  final que cita um total diferente) e o CSV.
- **F4** — tool `conferir_cupom` (`CUPOM_TOOLS`, módulo `files`, risco vazio)
  atrás da skill `skills/nota-fiscal` (tool_backed). As keywords (`notinha`, `nota
  fiscal`, `cupom fiscal`, `nfc-e`...) carregam a skill já no round 1
  (`autoload_for`). A anotação "é imagem, não vídeo" (`_AVISO_E_IMAGEM`) vai no
  resultado do `email_anexo`/`file_stage` quando `midia` é `tela` ou `conteudo`,
  uma vez por turno.
- Testes: `tests/test_continuacao_no_teto.py`, `tests/files/test_contas.py`,
  `tests/files/test_conferencia_no_file_save.py`,
  `tests/files/test_nota_fiscal_skill.py` (70 novos; a suíte passou de 4710 para
  4780 na cópia).
