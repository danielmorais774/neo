// Quem está trabalhando agora na conversa aberta, para o painel "Agentes em ação"
// da coluna esquerda (AgentActivityPanel). Plano: desktop-chat-tres-colunas-plan.md
// §3.2 e F4.
//
// Duas fontes:
//   (a) a frase de progresso do turno (evento `thinking`), casada pelo COMEÇO;
//   (b) as tarefas em background da conversa (`agent_job.kind`), estruturadas.
//
// (a) é frágil de propósito e com limite claro: o core manda só a frase pronta,
// sem o nome da tool. A tabela abaixo espelha `_TOOL_PROGRESS_LABEL` em
// neo-core/modules/desktop/ai_service.py (~linha 2095) e as duas frases fixas de
// WhatsApp ("Iniciando missão WhatsApp...", ~9995) e Home Assistant ("Falando com
// o Home Assistant...", ~10114). Se uma frase mudar lá, o pior caso é aquele
// agente não aparecer aqui — o chat não lê nada disto. A correção de vez é a F5:
// o core mandar o nome da tool junto com a frase.

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

// Em minúsculas; a frase do evento é comparada em minúsculas. A frase pode vir
// com alvo depois de dois-pontos ("abrindo a página: g1.globo.com"), daí o prefixo.
const PHRASE_PREFIXES: ReadonlyArray<readonly [string, AgentId]> = [
  // email_inbox, email_message, email_anexo, email_send
  ['olhando a caixa de email', 'email'],
  ['lendo o email', 'email'],
  ['pegando o anexo do email', 'email'],
  ['enviando o email', 'email'],
  // browser_local_task, browser_task
  ['dirigindo o seu navegador', 'browser'],
  ['abrindo o navegador do servidor', 'browser_server'],
  // web_search, web_fetch, deep_research
  ['pesquisando na web', 'web'],
  ['abrindo a página', 'web'],
  ['abrindo a pesquisa profunda', 'web'],
  // neo_bash, neo_write_file, neo_read_file, neo_list_files, file_stage
  ['rodando no sandbox', 'sandbox'],
  ['escrevendo no sandbox', 'sandbox'],
  ['lendo do sandbox', 'sandbox'],
  ['listando o sandbox', 'sandbox'],
  ['levando o arquivo pro sandbox', 'sandbox'],
  // file_info, file_read, file_list, file_save, deep_read
  ['conferindo o arquivo', 'files'],
  ['lendo o arquivo', 'files'],
  ['olhando os arquivos', 'files'],
  ['publicando o arquivo', 'files'],
  ['delegando a leitura dos documentos', 'files'],
  // wm_write, wm_read
  ['anotando no caderno', 'notebook'],
  ['relendo o caderno', 'notebook'],
  // claude_task, claude_reply, claude_sessoes, claude_enviar
  ['passando a tarefa para o claude code', 'claude'],
  ['falando com o claude code', 'claude'],
  ['olhando as sessões do claude no mac', 'claude'],
  ['mandando o pedido para a sessão do claude', 'claude'],
  // trilhos próprios, sem `cycle` (ai_service.py)
  ['iniciando missão whatsapp', 'whatsapp'],
  ['falando com o home assistant', 'home_assistant'],
];

export function agentForPhrase(text: string): AgentId | null {
  const phrase = text.trim().toLowerCase();
  if (!phrase) return null;
  for (const [prefix, agent] of PHRASE_PREFIXES) {
    if (phrase.startsWith(prefix)) return agent;
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
