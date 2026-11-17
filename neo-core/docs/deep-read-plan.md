# Leitura delegada de documentos (`deep_read`) — plano e spec

> Status: **implementado em 2026-09-16**; §10 (conferências + extensão) em 2026-09-17. Flag `DEEP_READ_ENABLED` (default `true`,
> exige `jobs_enabled` + workbench). Slot de LLM `document_reader` no painel.
> Nasceu da sessão `f33632b4` (relatórios anuais 2026).

## §1. O que a sessão f33632b4 mostrou

Pedido: "nessa pasta tem vários relatórios anuais, analisa carga tributária,
regulamentação e metas irrealistas, monta uma conclusão em .md". Seis
PDFs, ~1 MB de texto (Alfa 296 k chars, Bravo 164 k, Charlie 167 k…).

O que aconteceu, medido no log (4,2 MB para 3 interações):

| # | Defeito | Evidência |
|---|---|---|
| D1 | **Interação 1 não abriu um PDF.** Fez 3 `web_search` + 6 `web_fetch` e escreveu a "conclusão" sobre notícias. | `conclusao-custos-regulacao-metas.md` no Desktop, 7,8 KB, zero página citada |
| D2 | **Um arquivo por ida e volta.** `request_user_file` × 5, cada um retomando o turno com ~40 KB de prompt. | L6426–L8634 |
| D3 | **Foxtrot ficou de fora.** 6 PDFs listados, 5 pedidos. Nada no fluxo confere a lista. | `request_user_file` nunca recebeu `Relatorio_Foxtrot.pdf` |
| D4 | **Leitura dentro do contexto.** `head -c 8000`, um grep em Python com 63 KB de saída, um `neo_read_file` de 59,5 KB. O prompt chegou a **153 KB** num round. | L12538 (msg [20] 63 015 chars), L17951 (msg [19] 59 535 chars) |
| D5 | **Busca por palavra-chave no lugar de leitura.** O próprio modelo admitiu: "a busca por palavra-chave não capturou o conteúdo real". | L18619 |
| D6 | **O caderno virou depósito truncado.** Nota `sessao-f33632b4`: 49 KB, 37 entradas, trechos de PDF e a conclusão inteira cortados em 4 000 chars com `[…truncado]`. | `working_note.body` |
| D7 | **Afirmação falsa no fim.** "Executei o script… o resultado está em Desktop/conclusao-…md" — aquele arquivo era o da interação 1 (web). | L19936 |

D4–D6 são a mesma falha: o cérebro tentou ser o leitor. Um modelo com janela
finita, relendo tudo a cada round, não lê seis documentos de 80–200 páginas. E o
caderno herdou a leitura (`note_detail` de `neo_bash` e `neo_write_file`), o que é
errado duas vezes: estoura e fica incompleto.

## §2. A ideia

O cérebro **decide o que procurar**. Quem **lê** são subagentes, um documento por
vez e em paralelo, cada um com a janela limpa. O resultado vai para **arquivos**,
nunca para o contexto nem para o caderno:

```
/workspace/analises/<titulo>-<job8>/
  sintese.md          ← quadro comparativo + resumo e análise por pergunta de cada doc
  notas/<doc>.md      ← análise completa do doc, com citações LITERAIS conferidas e página
  estado.json         ← status por documento (máquina)
  .texto/             ← texto extraído e fatiado; apagado no fim
```

O caderno recebe só o endereço e o status — exatamente o formato do exemplo:

```
Leitura delegada (deep_read, job 3f2a1b9c) — comparar os relatórios anuais 2026
Pasta: /workspace/analises/relatorios-anuais-2026-3f2a1b9c/ → notas/<doc>.md + sintese.md
Status 16/09 17:52: 3/6 feitos (Relatorio_Charlie.pdf, …) · em andamento: … · pendentes: …
```

No fim, a **continuação** (`then`) lê `sintese.md` — 10–20 KB em vez de 1 MB — e
escreve a análise final. É o único passo que precisa ver o todo.

## §3. Por que um job, e não uma tool síncrona

- Seis documentos grandes levam minutos. Turno preso = timeout (ver
  neo-turn-liveness) e conversa bloqueada.
- O trilho de job já resolve o resto de graça: fila no banco, cancelamento
  (`job_cancel` e a janela Tarefas), `progress_note`/heartbeat, reconcile no boot,
  **continuação automática** (`then` → `scheduled_prompt`) e anotação do resultado
  (`ProactiveDispatcher._note_result`).
- Mesmo padrão do `deep_research`: `core/documents` é puro (recebe sandbox, LLM e
  progresso por protocolo) e `core/jobs/runner_deep_read.py` monta os adapters.

## §4. Pipeline

Para cada documento (até `DEEP_READ_DOC_CONCURRENCY`=3 ao mesmo tempo):

1. **Extração no sandbox** — `python3 /workspace/.neo/deep_read_extract.py`. PDF por
   `pdftotext` (ordem de leitura, não `-layout`, que intercala colunas), uma
   unidade por página (`\f`); PDF escaneado cai em OCR (`pytesseract`, `por`, teto
   de páginas); DOCX/PPTX/planilha/HTML/texto pelas libs da imagem. Limpa
   caracteres de controle (o Echo vinha com `\x1f\x1e…`). Fatia em partes de
   ~16 k chars **respeitando a página**, com marcadores `[[página N]]`.
2. **Mapa** — uma chamada por parte (`DEEP_READ_LLM_CONCURRENCY`=6 no job todo),
   temperatura 0,1, JSON, raciocínio desligado. Devolve `achados` com
   `pergunta`, `citacao` literal, `pagina` e `nota`.
3. **Aterramento (sem LLM)** — cada citação é procurada no texto da parte
   (normalizado: NFKC, hífen de quebra, espaços, aspas). Não bateu → descartada e
   contada. A página vem de ONDE a citação foi achada, não do que o modelo disse.
   É isso que impede o "conhecimento geral" de virar evidência.
4. **Redução** — uma chamada por documento, só sobre os achados conferidos (nunca
   o texto): `resumo`, e por pergunta `veredito` curto + `analise` citando páginas.
   Sem evidência para uma pergunta e cobertura 100% → "não encontrado (lido por
   inteiro)". Cobertura parcial é dita como parcial.
5. **Escrita** — `notas/<doc>.md` (determinístico a partir do JSON + a lista de
   evidências) e, sob lock, `estado.json` + `sintese.md` reescritos inteiros na
   ordem dos arquivos de entrada, + a nota rolante do caderno.

Falha de uma parte: 1 retentativa; se persistir, a parte conta como não lida e a
cobertura do documento diz isso. Falha de extração/redução: o documento vira
`falhou` com o motivo, os outros seguem.

## §5. Contrato da tool

`deep_read(files[], questions[], goal, title, instructions?, then?)`

- `files`: caminhos no sandbox (`/workspace/files/X.pdf`) **ou** `file_id` do
  catálogo (o runner materializa com `file_store.stage`). Caminho do disco do
  usuário (`/Users/…`, `~/…`) é recusado na hora com a instrução de trazer por
  `request_user_file(stage=true)`. Arquivo inexistente também é recusado **na
  enfileiração**, não 5 minutos depois.
- Repetição (mesma sessão, mesmos arquivos e perguntas, job vivo) devolve o job
  existente.
- Resultado imediato: `job_id`, a pasta e a nota "não leia os documentos você
  mesmo; avise e encerre".

## §6. Higiene do caderno e do contexto (vale para tudo, não só para o deep_read)

A regra da casa continua: **efêmero se guarda, relegível se aponta** (ver
working-memory-plan §14). O que estava furando:

- `neo_bash` de **leitura** (`cat`, `head`, `tail`, `sed -n`, `grep`, `rg`, `awk`,
  `less`, `strings`, `pdftotext … -`): o stdout é o arquivo — vai só o comando, que
  É o jeito de reler, e o tamanho. Saídas curtas (≤ 600 chars) continuam inteiras.
- `neo_bash` com stdout **acima do teto** do caderno: antes ia cortado com
  `[…truncado]`, o que parecia completo. Agora vai o começo + a frase explícita de
  que o resto não foi guardado.
- `neo_write_file`: o arquivo persiste no /workspace — vai caminho, tamanho e a
  primeira linha, não o conteúdo.
- `neo_read_file`: teto de 32 KB (era 60 KB) e a nota de truncamento aponta o
  `deep_read` para análise de documento grande.

## §7. Prompt

Bloco `_DEEP_READ_INSTRUCTION`, injetado só quando a tool está no toolset:
planejar antes de ler, trazer TODOS os arquivos (conferindo a listagem), uma
chamada de `deep_read`, avisar e encerrar; nunca "analisar" com `neo_read_file`/
`cat`/`grep` no texto inteiro nem responder com busca na web quando o pedido é
sobre os arquivos dele.

## §8. Decisões

- **Arquivos no /workspace, não Postgres.** O conteúdo não é para o prompt; é
  para ser lido por quem precisar, com `neo_read_file`, e entregue com `file_save`.
- **`.texto/` é apagado no fim.** É cópia integral do documento; notas e síntese
  são o produto (feedback: nada de cópia sobrando do conteúdo dele no servidor).
- **Aterramento determinístico.** Uma citação que não existe no texto é o
  defeito mais caro possível numa análise; checar custa um `in`.
- **Um slot só (`document_reader`)** para mapa e redução, default
  `openrouter` / `deepseek/deepseek-v4-flash@baidu` — a carteira viva (ver
  neo-llm-slots-and-wallets). A redução é pouca chamada; se a qualidade pedir,
  o painel troca.
- **A continuação não grava no disco do usuário.** Ela roda sem cliente
  (`unattended`), então entrega por `file_save` (botão). Colocar numa pasta dele
  continua sendo pedido explícito no turno seguinte.

## §9. Fora do escopo (próximos passos)

- **Vários arquivos num `request_user_file`** (D2): exige o desktop aceitar `paths[]`
  (Rust, `run_request_user_file`) — hoje é um por ida e volta.
- **D7** (afirmar entrega sobre arquivo antigo do disco do usuário): a guarda de
  artefato só olha o sandbox.
- Perguntas de acompanhamento sobre a mesma pasta reusando as notas sem reler.

## §10. O primeiro teste real (sessão ad3be3bf) e o que mudou

Mesmo pedido dos relatórios anuais. A leitura funcionou (5 documentos, 568
citações, 5 min), mas a entrega falhou em duas rodadas:

| # | Defeito | Causa |
|---|---|---|
| E1 | **Foxtrot fora** da 1ª conclusão | inventário feito no `/workspace/files` (cópias da sessão f33632b4, 5 de 6), não na pasta que o usuário citou |
| E2 | **Echo fora** da 1ª conclusão | `sintese.md` com 42,8 KB; o teto de 32 KB do `neo_read_file` (§6) cortou no 4º documento e o cérebro ignorou o aviso de truncado |
| E3 | "Sem menção específica" em regulação para **todos** | o cérebro trocou os 3 critérios do usuário por 9 eixos genéricos (economia, social, segurança…) — nenhum sobre regulação |
| E4 | citações sem página ("5 páginas" para 5 planos) | leu os `.txt` extraídos na sessão anterior, não os PDFs |
| E5 | **correção apagou conclusões** | "faltou o Foxtrot" abriu uma leitura NOVA (outra pasta, outras perguntas) e a continuação escreveu `-com-foxtrot.md` do zero, a partir de duas sínteses que não conversavam — sem ler a conclusão anterior |

Consertos (todos determinísticos, exceto o verificador de critérios):

- **Síntese com orçamento** (`SYNTHESIS_MAX_BYTES = 26 000`): compacta análise e
  resumo em degraus; quadro e vereditos de TODOS os documentos ficam sempre. O que
  foi cortado continua na nota. (E2)
- **`source_dir` conferido contra a listagem real** (`deep_read_tool.py`): se o
  usuário citou uma pasta, a tool exige `source_dir`; sem `ls` daquela pasta nesta
  conversa (evidência de disco do AIService) ela manda listar; documento da pasta
  fora de `files` é recusado com o nome (`excluded` só para o que o usuário mandou
  deixar de fora). Índice pequeno (FONTES.md, 3 KB) não é exigido. (E1)
- **`.txt` com PDF irmão vira o PDF.** (E4)
- **Verificador de critérios** (`AIService._deep_read_criteria_gap`, 1 chamada,
  fail-open): mapeia cada critério do usuário para a pergunta que o cobre; sem
  pergunta → recusa. Medido: recusa os 9 eixos da ad3be3bf, aceita paráfrases, no
  caso misto aponta só o que falta. Formato "liste o que falta" era literal demais
  (acusava "Metas irreais" de não cobrir "metas irrealistas"). (E3)
- **`extend`**: mesma pasta, mesmas perguntas + as novas, só o novo é lido. Cada
  documento pronto grava `.dados/<doc>.json` (respostas, evidências, perguntas
  respondidas); na extensão ele volta sem LLM e, se houver pergunta nova, é relido
  SÓ para ela e as respostas são mescladas. Medido em prod: Echo+Charlie mantidos com
  0 chamadas, Foxtrot lido, síntese com os 3. (E5)
- **Conclusão já entregue é localizada no banco** (continuação `scheduled_prompt`
  com `continuation_of` → `extra_metadata.files` → `stored_file.meta_json.source.
  workspace_path`) e vai em `entregas_anteriores`. O **contrato da continuação**
  (`render_continuation_contract`, anexado pelo JobManager) manda: cobrir TODOS os N
  documentos e perguntas, ler a síntese inteira, e — havendo conclusão anterior —
  ATUALIZAR o mesmo arquivo em vez de escrever outro. (E5)

Ainda não é garantido por código: que a continuação realmente edite o arquivo
anterior em vez de reescrevê-lo pior. O contrato diz; nada confere o conteúdo.

