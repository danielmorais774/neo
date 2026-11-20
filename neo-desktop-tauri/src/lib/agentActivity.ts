// Quem está trabalhando agora na conversa aberta, para o painel "Agentes em ação"
// da coluna esquerda (AgentActivityPanel). Plano: desktop-chat-tres-colunas-plan.md
// §3.2 e F4.
//
// Duas fontes:
//   (a) a frase de progresso do turno (evento `thinking`);
//   (b) as tarefas em background da conversa (`agent_job.kind`), estruturadas.
//
// (a) é frágil de propósito e com limite claro: o core manda só a frase pronta.
// Ela tem três formas, todas em neo-core/modules/desktop/ai_service.py:
//   1. trilho do cérebro: o rótulo de `_TOOL_PROGRESS_LABEL` (~linha 2095),
//      "olhando a caixa de email", às vezes com alvo ("abrindo a página: g1…");
//   2. trilho do servidor (SERVER_TOOL): UMA frase com o nome cru da tool,
//      "Executando ferramenta no servidor: email_inbox..." (~9683). O agente desse
//      trilho (AgentExecutor) não manda progresso por passo, então o rótulo fica
//      parado até o turno acabar (session-0e232e1b);
//   3. trilho do Mac (CLIENT_TOOL): "Solicitando execução de run_command no
//      desktop..." (~11307) e as frases fixas do terminal, WhatsApp e Home
//      Assistant.
// Se uma frase mudar lá, o pior caso é aquele agente não aparecer aqui — o chat
// não lê nada disto. A correção de vez é a F5: o core mandar o nome da tool
// junto com o progresso.

export type AgentId =
  | 'email'
  | 'browser'
  | 'browser_server'
  | 'web'
  | 'sandbox'
  | 'files'
  | 'notebook'
  | 'claude'
  | 'whatsapp'
  | 'home_assistant'
  | 'computer';

export const AGENT_NAME: Record<AgentId, string> = {
  email: 'Email',
  browser: 'Navegador',
  browser_server: 'Navegador do servidor',
  web: 'Web',
  sandbox: 'Sandbox',
  files: 'Arquivos',
  notebook: 'Caderno',
  claude: 'Claude Code',
  whatsapp: 'WhatsApp',
  home_assistant: 'Home Assistant',
  computer: 'Seu computador',
};

// tool → agente e o rótulo de `_TOOL_PROGRESS_LABEL` (cópia literal). Tools que não
// são "alguém trabalhando" (clima, memória, skill) ficam de fora.
const TOOLS: Record<string, readonly [AgentId, string]> = {
  email_inbox: ['email', 'olhando a caixa de email'],
  email_message: ['email', 'lendo o email'],
  email_anexo: ['email', 'pegando o anexo do email'],
  email_send: ['email', 'enviando o email'],
  browser_local_task: ['browser', 'dirigindo o seu navegador'],
  browser_task: ['browser_server', 'abrindo o navegador do servidor'],
  web_search: ['web', 'pesquisando na web'],
  web_fetch: ['web', 'abrindo a página'],
  deep_research: ['web', 'abrindo a pesquisa profunda'],
  neo_bash: ['sandbox', 'rodando no sandbox'],
  neo_write_file: ['sandbox', 'escrevendo no sandbox'],
  neo_read_file: ['sandbox', 'lendo do sandbox'],
  neo_list_files: ['sandbox', 'listando o sandbox'],
  file_stage: ['sandbox', 'levando o arquivo pro sandbox'],
  neo_transcrever: ['sandbox', 'transcrevendo o áudio'],
  neo_video_candidatos: ['sandbox', 'comparando os vídeos encontrados'],
  file_info: ['files', 'conferindo o arquivo'],
  file_read: ['files', 'lendo o arquivo'],
  file_list: ['files', 'olhando os arquivos'],
  file_save: ['files', 'publicando o arquivo'],
  deep_read: ['files', 'delegando a leitura dos documentos'],
  wm_write: ['notebook', 'anotando no caderno'],
  wm_read: ['notebook', 'relendo o caderno'],
  claude_task: ['claude', 'passando a tarefa para o Claude Code'],
  claude_reply: ['claude', 'falando com o Claude Code'],
  claude_sessoes: ['claude', 'olhando as sessões do Claude no Mac'],
  claude_enviar: ['claude', 'mandando o pedido para a sessão do Claude'],
  // Tools do cliente (CLIENT_TOOL): rodam no Mac, sem rótulo no core.
  run_command: ['computer', 'rodando um comando no seu terminal'],
  read_user_file: ['computer', 'lendo um arquivo do seu computador'],
  edit_user_file: ['computer', 'editando um arquivo do seu computador'],
  write_file: ['computer', 'gravando um arquivo no seu computador'],
  delegate_file_edit: ['computer', 'editando um arquivo do seu computador'],
  request_user_file: ['computer', 'pegando um arquivo do seu computador'],
  save_generated_file: ['computer', 'salvando o arquivo no seu computador'],
};

// Frases fixas que não são rótulo de tool (ai_service.py).
const FIXED_PHRASES: ReadonlyArray<readonly [string, AgentId]> = [
  ['iniciando missão whatsapp', 'whatsapp'],
  ['falando com o home assistant', 'home_assistant'],
  ['vou rodar um comando no seu terminal', 'computer'],
  ['rodando o comando no seu terminal', 'computer'],
];

// Em minúsculas: a frase do evento é comparada em minúsculas. Prefixo porque o
// rótulo pode vir com alvo depois de dois-pontos.
const PHRASE_PREFIXES: ReadonlyArray<readonly [string, AgentId]> = [
  ...Object.values(TOOLS).map(([agent, label]) => [label.toLowerCase(), agent] as const),
  ...FIXED_PHRASES,
];

// As duas frases que trazem o NOME da tool: trilho do servidor e trilho do Mac.
// Nome de tool é snake_case: sem o ponto no grupo, o "..." do fim não gruda nele.
const SERVER_TOOL_RE = /^executando ferramenta no servidor:\s*(\w+)/i;
const CLIENT_TOOL_RE = /^solicitando execução de\s+(\w+)\s+no desktop/i;

export interface PhraseMatch {
  agent: AgentId;
  /** O que mostrar como passo atual: o rótulo humano, nunca o nome cru da tool. */
  step: string;
}

export function agentForPhrase(text: string): PhraseMatch | null {
  const phrase = text.trim();
  if (!phrase) return null;

  const named = SERVER_TOOL_RE.exec(phrase) ?? CLIENT_TOOL_RE.exec(phrase);
  if (named) {
    const tool = TOOLS[named[1]];
    return tool ? { agent: tool[0], step: tool[1] } : null;
  }

  const lower = phrase.toLowerCase();
  for (const [prefix, agent] of PHRASE_PREFIXES) {
    if (lower.startsWith(prefix)) return { agent, step: phrase.replace(/\.{3}$|…$/, '') };
  }
  return null;
}

/** Tipo de tarefa em background → agente. Tipos que não são "alguém
 *  trabalhando" (agenda, lembrete, vigia de download) ficam de fora. */
export const AGENT_FOR_JOB_KIND: Record<string, AgentId> = {
  browser_local: 'browser',
  browser: 'browser_server',
  sandbox_task: 'sandbox',
  claude_code: 'claude',
  research: 'web',
  comando_local: 'computer',
};
