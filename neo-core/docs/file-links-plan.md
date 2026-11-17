# Conciliação de arquivos entre o disco do usuário e o sandbox — registro de vínculos

> Status: **implementado em 2026-09-17** (tabela `file_link`, migração
> `z9a0b1c2d3e4` aplicada em prod). Sem flag: liga junto com o File Store.
> Nasceu da sessão `f33632b4`, interações 4–10.

## §1. O que aconteceu

Depois da análise dos relatórios anuais, o usuário pediu para refinar a parte de
regulação, juntar ao "arquivo final" e colocar numa pasta. Sete turnos, nada entregue:

| # | Pedido | O que o Neo fez | Por quê |
|---|---|---|---|
| 5 | "adiciona ao arquivo final e coloque em ~/Desktop/teste-de-analise" | classificador → `save_generated_file` com o **file_id do Relatorio_Charlie.pdf** e o nome da conclusão `.md`. "Pronto, salvei." | o id mais recente do histórico era o do PDF que tinha subido; nada confere se o id é o documento que o nome diz; o "adiciona" sumiu |
| 6 | "parece corrompido" | `cat` → `%PDF-1.4` | — |
| 7 | "ele está com extensão .md" | vagou: releu cópias do sandbox, `mkdir`, `ls`, subiu outro PDF, terminou prometendo | não havia lugar nenhum dizendo qual cópia é qual |
| 8 | "delete o arquivo e coloca um novo em markdown" | `rm /workspace/...` na máquina dele → bloqueado → "baixe pelo botão" | a trava sandbox≠disco era beco sem saída |
| 9 | "baixe ele para mim e coloque lá" | heredoc python que **escrevia no Mac um markdown com texto inventado** pelo classificador → bloqueado (só porque citava /workspace) | shell que grava texto não ditado passava |
| 10 | "me mostra o botão de download" | plano de 3 `run_command`, todos falharam | não existia jeito de oferecer um arquivo já catalogado |

E dois furos que não aparecem na tela:

- `FilesExecutor.set_context` **nunca é chamado**: todo `file_save` do Neo entra no
  catálogo sem usuário e sem sessão. "A conclusão desta conversa" não tinha como
  ser encontrada.
- o "já vi esse arquivo" que libera `overwrite` vive em RAM (`_session_seen_files`) e
  some a cada reload do core — que acontece a cada `.py` salvo.

## §2. A peça: `file_link` (registro de vínculos)

Uma linha por **movimento** de arquivo entre os três lugares, com o `doc_key` do
documento lógico:

| ação | de → para | quem grava |
|---|---|---|
| `pulled` | disco → catálogo | retomada do `request_user_file` |
| `staged` | catálogo → sandbox | `file_stage` / auto-stage |
| `saved` | sandbox → catálogo | `file_save` (e carimba dono/sessão no `stored_file`) |
| `edited_sandbox` | sandbox mudou sem publicar | `neo_write_file` num caminho que JÁ é cópia de documento |
| `delivered` | catálogo → disco | `save_generated_file` (`wrong_content` se a extensão do arquivo ≠ a do destino) |
| `written_host` | texto gravado no disco | `write_file` (liga o `source_path` do sandbox) |
| `read_host` / `edited_host` | direto no disco | `read_user_file` / `edit_user_file` / `delegate_file_edit` |
| `deleted_host` | apagado do disco | `rm` de caminho já conhecido pelo registro |

Tudo passa por um ponto só — `_note_trace_step`, o mesmo do caderno — porque é por
ali que chegam os dois lados: as tools do laço cognitivo e os resultados do desktop.

**Identidade do documento** (`resolve_doc_key`): publicar de novo o mesmo caminho do
sandbox é versão nova do MESMO documento; o que foi entregue é o conteúdo daquele
`file_id` (se era o PDF, a cópia é do PDF, com o nome que tiver); mexer no disco
segue o caminho. Um caminho do disco pertence ao documento do evento mais recente
nele — consertar a cópia errada move ela do PDF para a conclusão.

**Visão** (`build_views`): por documento, a versão atual (sandbox, catálogo ou disco
dele), o estado de cada cópia no disco (`igual` / `desatualizada` / `atual` /
`errada` / `apagada`) e da cópia do sandbox (`atual` / `desatualizada` /
`nao_publicada`). Leitura com sha diferente do último conhecido = o usuário mexeu
por fora → a do disco vira a atual.

**Bloco** (`render_block`): "ARQUIVOS DESTA CONVERSA — onde está cada cópia", com
file_ids completos e as regras de uso, no `system_context` do turno E da retomada —
chega ao classificador e ao cérebro.

## §3. Travas (determinísticas)

1. **Arquivo ≠ nome** (`_delivery_identity`, classificador e cérebro): a extensão do
   `file_name` pedido tem de bater com a do arquivo do catálogo. Não bate → procura
   o documento pelo nome (registro primeiro, catálogo depois) e troca o id; sem
   achar → classificador desvia ao cérebro, cérebro recebe a recusa com o motivo.
2. **/workspace no comando do classificador** deixa de ser beco sem saída: vira turno
   do cérebro com o registro no contexto.
3. **Roteador** (`_reject_classifier_file_authoring`): `save_generated_file` com
   pedido de mudança de conteúdo ("adiciona", "atualiza", "corrige"…) → cérebro;
   `run_command` que grava arquivo (heredoc, `open(...,'w')`, `tee`, `>`) com texto
   não ditado → cérebro; "botão de download" → cérebro.
4. **overwrite** vale se a cópia está no registro (`_may_overwrite`), não só na RAM.
5. **`file_offer(file_id)`**: botão de download de algo já catalogado, sem publicar
   de novo.

## §4. Regras de comportamento (no bloco e no prompt do cérebro)

- "o arquivo" / "o arquivo final" = documento do registro; ids e caminhos saem de lá.
- Entregar: `save_generated_file` com o file_id da versão atual; cópia listada →
  `overwrite=true`.
- Editar "no meu arquivo" / "direto na minha máquina" / versão atual no disco →
  `read_user_file` + `edit_user_file` no caminho do disco, sem sandbox.
- Trabalho que precisa do sandbox termina devolvendo a versão nova às cópias dele.
- Cópia do sandbox desatualizada → puxar de volta antes de editar lá.

## §5. Validação

- `tests/files/test_file_ledger.py` (9): a sequência real da f33632b4, conserto por
  cima, edição no host, alteração não publicada, versão nova, mudança externa, rm.
- `tests/files/test_file_links_integration.py` (16): registro a partir dos passos,
  troca do id do PDF pelo da conclusão, desvio ao cérebro com o bloco, overwrite após
  reload, as 3 guardas do roteador, `file_offer`.
- Prod: `FileLedger` gravou, montou o bloco e limpou contra o Postgres real.

## §6. Não coberto

- Pedido pelo classificador que roda `rm` direto (sem cérebro) não passa por
  `_note_trace_step` — só o `rm` do cérebro é registrado.
- Sessão f33632b4 não tem registro (começa a contar agora).
- E2E pelo desktop não exercitado.

## §7. Segunda rodada (2026-09-17, sessões f33632b4 → 9b0b10cc)

O registro nem chegou a ser usado: a conversa foi parar em OUTRA sessão.

| # | O que aconteceu | Causa | Conserto |
|---|---|---|---|
| R1 | "tenta salvar o arquivo lá agora" (26 min parado) rotacionou a sessão | similaridade 0,398 < 0,45; "lá"/"o arquivo" não eram marcadores de continuação | `_DEICTIC_FOLLOWUP_RE` em `detect_context_dependency` (lá, ali, de novo, o/esse arquivo, essa pasta, "tenta salvar/…") |
| R2 | a rotação caiu numa conversa sobre o CLIMA da véspera | `get_or_create_session(session_id=None)` devolve qualquer sessão ATIVA do usuário | `_new_session_after_rotation` cria sempre uma nova |
| R3 | o `ls` da pasta chegou ao desktop sem comando ("comando ausente") | o gRPC guardava o pendente pelo id que o desktop mandou; a aprovação voltou com o id da sessão nova | `_track_pending`/`_take_pending` (mesmo dicionário nas duas chaves) + `process_command_approval` recupera o comando do estado de retomada |
| R4 | o cérebro respondeu "a pasta não existe" (existia) | leu falha de transporte como resposta do disco | `_is_transport_failure` → nota "o comando NÃO rodou, não conclua nada" |
| R5 | "coloca lá" com ids inventados (`file_1234567890abcdef`) e com o NOME (`conclusao-relatorios-anuais-2026.md`) → "não achei, peça de novo" 4× | o arquivo só existia no sandbox, nunca passou por `file_save`; a trava era beco sem saída | `_publish_sandbox_file_by_name`: nome exato com UMA ocorrência no /workspace é publicado e entregue; senão o cérebro assume (classificador e cérebro) |
| R6 | na sessão nova o Neo não sabia nada dos arquivos | registro era por sessão | `FileLedger.events(session_id, user_id=…)`: movimentos do usuário nas últimas 72 h entram também, e a identidade do documento continua entre sessões |
| R7 | desvio ao cérebro respondeu sobre o clima | consequência de R1/R2 (histórico era o do clima) | — |

Validado: 14 testes em `tests/test_rotation_transport_file_delivery.py`; em prod, o
registro de uma sessão antiga aparece na nova pelo usuário e a entrega nova cai no
mesmo documento.


## §8. Terceira rodada: dois anéis e versões (2026-11-10, session-20cb02b6)

O R6 trouxe o contrário do problema: numa conversa NOVA, sobre o template MAPA de
Processos de Software que ele anexou, o bloco "ARQUIVOS DESTA CONVERSA" mostrava um
TXT de questões de semiótica de outra conversa (08/11), com a regra "na dúvida, o
gerado mais recente". O Neo batizou o anexo de `questoes.docx`, no turno 2 reabriu o
original em branco (perdendo o turno 1) e no turno 3 propôs encher o template com
aquelas questões. O TXT chegou por 5 canais; este registro era o principal.

| # | Canal / defeito | Conserto |
|---|---|---|
| A1 | registro de 72 h sob "desta conversa" + "o mais recente" | dois anéis em `render_block`: desta conversa (uso livre) e de OUTRAS (só entra citado pelo nome/assunto — `ancorado` — ou quando esta não tem documento nenhum, como "pergunte qual"). Citado = `adopted` e vira desta nos turnos seguintes. "faz isso" depois de uma pergunta do Neo cita o que a pergunta citou (`texto_de_ancora`) |
| A2 | anexo aparecia como "gerado por você, file_id=None (?)" | evento `attached` gravado no manifesto do anexo (`_record_attachments`): origem "veio DELE", `original_file_id`/`original_sandbox_path` na visão |
| A3 | `questoes_preenchido.docx` era outro documento, sem ligação com o anexo | `file_save(versao_de=…)` + palpite pelo nome com UM candidato (`versao_provavel`): a versão preenchida é a versão 2 do anexo, e "esse arquivo" passa a ser ela |
| A4 | o Neo LEU o TXT com `cd /workspace/files && head …` sem ninguém avisar | `FileLedger.nota_de_origem` via `annotate_result` do laço: `file_read`/`file_stage`/`neo_read_file`/`neo_bash` que tocam documento de outra conversa recebem "⚠ ORIGEM: … confirme antes de usar no entregável" |
| A5 | "[memória]" sem origem | `rotulo_de_memoria`: `[memória · outra conversa · 05/11]` |
| A6 | resultados de job gravados como "exemplo de estilo" no bloco de Alta Prioridade | `PKSService._mensagem_sintetica` pula o aprendizado de "[Continuação automática"; limpeza do que já entrou: `scripts/pks_limpar_exemplos_de_job.py` (dry-run por padrão) |

Junto, para o mesmo caso: skill `docx` (map/preencher/conferir/renderizar), tool
`file_olhar` (visão), guarda de valor afirmado (`core/files/conferencia.py`) e
temperatura `[cerebro]` no `neo.toml`. Testes: `tests/files/test_ledger_aneis.py`,
`test_conferencia_de_valor.py`, `test_file_olhar.py`, `tests/skills/test_docx_scripts.py`.

Não coberto: sandbox por conversa (o `ls` de `/workspace/files` continua mostrando
tudo — o aviso de origem cobre o USO, não a visão); projetos (memória compartilhada
por assunto, à la ChatGPT/Claude Projects).
