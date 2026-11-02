// Tauri API wrapper - replaces Electron IPC with Tauri invoke/listen

import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { emit } from '@tauri-apps/api/event';
import type { AudioRoute } from './audioDevices';

// Types matching the original ElectronAPI interface
/** Tetos do navegador headless local. `0` em max_tabs/max_ram_mb = sem teto NOSSO. */
export interface HeadlessLimits {
  max_tabs: number;
  max_ram_mb: number;
  idle_ttl_s: number;
  profile: 'dedicado' | 'logado';
  browser_path: string | null;
  /** Existe Chromium instalado nesta máquina? */
  available: boolean;
}

export interface HeadlessLimitsInput {
  maxTabs: number;
  maxRamMb: number;
  idleTtlS: number;
  profile: 'dedicado' | 'logado';
  browserPath: string | null;
}

export interface HeadlessStats {
  running: boolean;
  browser?: string;
  pid?: number;
  uptime_s?: number;
  /** phys_footprint da árvore de processos — NÃO soma de RSS. */
  ram_mb?: number;
  tabs?: number;
  tab_urls?: string[];
  limits: { max_tabs: number; max_ram_mb: number; idle_ttl_s: number };
}

export interface AgentEvent {
  type: string;
  data: any;
  timestamp?: string;
}

export interface DeviceInfo {
  device_id: string;
  /** Nome exibido — é por ele que o cérebro endereça o aparelho. */
  name?: string;
  display_name?: string | null;
  device_type: string;
  model: string;
  hardware_id: string;
  os_platform: string;
  os_version: string;
  client_version: string;
  ip_address: string;
  connected_at_unix: number;
  is_current_device: boolean;
  is_online?: boolean;
  /** Canal de controle aberto — mais estrito que is_online (presença). */
  controllable?: boolean;
  capabilities?: Record<string, boolean | Record<string, boolean>>;
  /**
   * A MÁQUINA onde esta linha roda, quando ela é uma superfície de navegador
   * (neo-core/docs/browser-surface-plan.md). Preenchido no pareamento, pelo
   * desktop — a extensão não pode se declarar "do macbook".
   */
  host_device_id?: string | null;
}

export interface NotificationAction {
  action_type: string;
  label: string;
  payload?: Record<string, unknown>;
}

export interface NotificationField {
  label: string;
  value: string;
}

export interface NotificationConfig {
  duration_ms?: number;
  position?: 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right' | 'center';
  persistent?: boolean;
  play_sound?: boolean;
}

export interface NeoNotification {
  id: string;
  userId: string;
  missionId: string | null;
  kind: string;
  title: string;
  description: string;
  fields?: NotificationField[];
  actions?: NotificationAction[];
  payload: Record<string, unknown>;
  createdAtUnix: number;
  config?: NotificationConfig;
}

export interface NotificationActionEvent {
  notification_id: string;
  action_type: string;
  action_payload?: Record<string, unknown>;
}

/** Um microfone visível ao CoreAudio. `name` é a chave comum aos três processos
 *  (webview, notch nativo e sidecar da wake word). */
export interface AudioInputDevice {
  name: string;
  /** 'builtin' | 'bluetooth' | 'usb' | 'other' */
  transport: string;
}

export interface VoiceConfig {
  wsBaseUrl: string;
  apiKey: string | null;
  voice: string | null;
  backend: string;
  language: string | null;
  /** Segundos ociosos (escutando, ninguém falando) até o Modo Live se desligar
   *  sozinho na janela principal; 0 = sem timeout. Ver NEO_LIVE_IDLE_TIMEOUT_S. */
  liveIdleTimeoutS: number;
  /** Idem para o chip nativo do notch — mais curto, consumido pelo Swift. */
  notchIdleTimeoutS: number;
  /** Microfone escolhido pelo usuário, por nome; null = padrão do sistema. */
  inputDeviceName: string | null;
}

// Engine de voz (STT/TTS) controlável no PC gamer via os scripts engine_ctl.
export interface VoiceEngine {
  key: string;
  label: string;
  kind: string; // "Windows" | "WSL2"
  port: number;
  backend: string | null; // id do backend no neo-voice (null p/ o STT)
}

export type VoiceEngineAction = 'start' | 'stop' | 'restart' | 'status';

// Estado do detector de wake word local (Estágio 1 do Modo Live).
export interface WakeStatus {
  running: boolean;
  python: string;
  script: string;
  refsDir: string;
  threshold: number;
  cooldownMs: number;
}

// Evento `wake-detected`: o sidecar reconheceu a palavra-gatilho.
export interface WakeDetected {
  model: string;
  score: number;
}

// Frase-gatilho cadastrada (few-shot pela voz do usuário).
export interface WakePhrase {
  slug: string;
  label: string;
  sampleCount: number;
  enrolled: boolean; // referência (embeddings) já gerada
  minSamples: number; // mínimo exigido p/ gerar a referência
  role: 'wake' | 'stop'; // "wake" acende o Live; "stop" ("pare") interrompe
}

// Active voice resolved by neo-core (from the DB) and carried on the agent's
// final reply (Pattern A). When present it overrides the static VoiceConfig for
// that utterance. `engine` maps to the neo-voice `backend` field.
export interface VoiceOverride {
  engine?: string | null;
  voice?: string | null;
  language?: string | null;
  label?: string | null;
}

// Voice imitation: one chunk of a multi-voice reply, carried on the agent's
// `complete` event as `segments`. Spoken with `voice` if set, otherwise with the
// reply-level default voice (VoiceOverride above).
export interface SpeechSegmentOverride {
  text: string;
  voice?: VoiceOverride | null;
}

// Registro do catálogo devolvido pelo File Store no upload (neo-core
// api/routes/files.py). Só o que a UI usa.
export interface StoredFileMeta {
  id: string;
  name: string;
  mime: string | null;
  size_bytes: number;
  retention: string;
  expires_at: string | null;
}

// Imagem carregada para exibir na bolha do chat (marcador `[[IMAGE:…]]`). Os
// bytes vêm do disco do usuário (ou do File Store) via Rust, já como data URL —
// nunca passam pelo prompt/LLM. Ver src-tauri: load_chat_image.
export interface ChatImage {
  dataUrl: string;
  name: string;
  mime: string;
  sizeBytes: number;
  /** Caminho absoluto real; ausente quando a imagem veio do catálogo. */
  path?: string | null;
}

/** Turno que veio do microfone (STT do neo-voice), não do teclado.
 *  Vira `input_source`/`asr_raw`/`asr_engine` no metadata do gRPC — é o que faz o
 *  neo-core tolerar erro de transcrição (docs/voice-input-normalization-plan.md). */
export interface VoiceInputSource {
  source: 'voice';
  /** transcrição antes da correção do nome no neo-voice */
  raw?: string;
  engine?: string;
}

export interface ElectronAPI {
  sendMessage: (message: string, model?: string, mode?: string) => Promise<unknown>;
  executeWithLoop: (
    message: string,
    model?: string,
    mode?: string,
    provider?: string,
    attachments?: string[],
    voice?: VoiceInputSource,
    /** Quantos caracteres desta mensagem vieram do CLIPBOARD. Vira
     *  `pasted_chars` no metadata do gRPC: acima do limiar o neo-core marca o
     *  turno como contaminado (fonte `colagem`) e revelar passa a pedir o card
     *  de liberação. Só o cliente sabe disso — o core só vê o texto final.
     *  Ver neo-core/docs/release-request-plan.md §21. */
    pastedChars?: number
  ) => Promise<void>;
  // Anexos: picker nativo → upload pro File Store do neo-core. O `id` que volta
  // é o que se manda em `attachments` no envio da mensagem.
  pickAttachmentFiles: () => Promise<string[]>;
  uploadAttachment: (path: string) => Promise<StoredFileMeta>;
  // Arquivo que o Neo gerou/catalogou (file_save) — baixa do neo-core e salva
  // localmente via diálogo nativo "Salvar como". Resolve com o path salvo;
  // rejeita com "cancelled" se o usuário fechou o diálogo sem escolher.
  downloadGeneratedFile: (fileId: string, fileName: string) => Promise<string>;
  // Imagem inline: `source` é o conteúdo do marcador `[[IMAGE:source]]` — um
  // caminho do disco do usuário ou `file:<file_id>` do catálogo. Rejeita com uma
  // mensagem em português quando o caminho não existe / não é imagem exibível.
  loadChatImage: (source: string) => Promise<ChatImage>;
  // Abre o arquivo no app padrão do sistema (ver a imagem em tamanho real).
  revealLocalFile: (path: string) => Promise<void>;
  respondFileChange: (changeId: string, approved: boolean) => Promise<void>;
  undoFileChange: (changeId: string) => Promise<void>;
  stopExecution: () => void;
  resetAgent: (newSessionId: string) => Promise<string>;
  onAgentEvent: (callback: (event: AgentEvent) => void) => UnlistenFn;
  executeCommand: (command: string) => Promise<{ success: boolean; output?: string; error?: string }>;
  onCommandRequest: (callback: (command: string, explanation: string) => void) => UnlistenFn;
  respondToPermission: (requestId: string, approved: boolean) => void;
  onPasswordRequest: (callback: (prompt: string) => void) => UnlistenFn;
  respondToPassword: (password: string | null) => void;
  getConversationHistory: () => Promise<unknown[]>;
  clearHistory: () => Promise<void>;
  saveConversationHistory: (messages: unknown[]) => Promise<void>;
  getWindowMode: () => Promise<'tooltip' | 'agent'>;
  getNeoCoreStatus: () => Promise<{
    connected: boolean;
    status: string;
    service?: string;
    checkedAt: string;
  }>;
  onAppStarted: (callback: (payload: unknown) => void) => UnlistenFn;
  minimizeWindow: () => void;
  closeWindow: () => void;
  onScreenCapture: (callback: (data: { imageDataUrl: string; question: string }) => void) => UnlistenFn;
  quickHelpAskStream: (question: string) => void;
  onQuickHelpChunk: (cb: (chunk: string) => void) => UnlistenFn;
  onQuickHelpDone: (cb: () => void) => UnlistenFn;
  onQuickHelpError: (cb: (msg: string) => void) => UnlistenFn;
  quickHelpResize: (expanded: boolean) => void;
  quickHelpMoveBy: (dx: number, dy: number) => void;
  onNotification: (callback: (notification: NeoNotification) => void) => UnlistenFn;
  ackNotification: (notificationId: string) => Promise<{ ok: boolean; error?: string }>;
  onNotificationAction: (callback: (event: NotificationActionEvent) => void) => UnlistenFn;
  listConnectedDevices: () => Promise<DeviceInfo[]>;
  devicePairingStatus: () => Promise<{ paired: boolean; device_id: string }>;
  pairDevice: (code: string, displayName?: string) => Promise<string>;
  renameDevice: (targetDeviceId: string, displayName: string | null) => Promise<void>;
  /** Resolve com true quando o removido foi ESTE aparelho (= despareou). */
  removeDevice: (targetDeviceId: string) => Promise<boolean>;
  setNotificationContext: (activeTab: 'chat' | 'devices') => Promise<void>;
  getVoiceConfig: () => Promise<VoiceConfig>;
  // Motores de voz no PC gamer (subir/baixar via engine_ctl por SSH).
  listVoiceEngines: () => Promise<VoiceEngine[]>;
  voiceEngineHealth: (key: string) => Promise<boolean>;
  voiceEngineAction: (key: string, action: VoiceEngineAction) => Promise<string>;
  // Rota de áudio (CoreAudio): diz se o AEC é necessário e qual mic preferir.
  audioRoute: () => Promise<AudioRoute>;
  /** Microfones disponíveis agora, vistos pelo CoreAudio (nome + transporte). */
  listAudioInputs: () => Promise<AudioInputDevice[]>;
  /** Microfone escolhido pelo usuário, por nome; null = padrão do sistema. */
  getAudioInput: () => Promise<string | null>;
  /** Persiste a escolha (null limpa) e reenvia o config ao notch nativo. */
  setAudioInput: (name: string | null) => Promise<void>;
  // Wake word local (Estágio 1 do Modo Live).
  // Navegador headless LOCAL (services/headless.rs): mesmo IP residencial do
  // navegador visível dele, sem tomar a tela. Os tetos de RAM/abas são daqui
  // porque é a máquina DELE que paga a conta.
  headlessStats: () => Promise<HeadlessStats>;
  headlessGetLimits: () => Promise<HeadlessLimits>;
  headlessSetLimits: (limits: Partial<HeadlessLimitsInput>) => Promise<HeadlessLimits>;
  headlessShutdown: () => Promise<{ ok: boolean }>;
  wakeStart: () => Promise<WakeStatus>;
  wakeStop: () => Promise<WakeStatus>;
  wakeStatus: () => Promise<WakeStatus>;
  onWakeDetected: (callback: (detected: WakeDetected) => void) => UnlistenFn;
  // Monitor de teste: score de cada fala tipo-wake (inclui near-misses abaixo do
  // limiar). Usado só pela tela "Teste de Wake" nas Configurações.
  onWakeScore: (callback: (detected: WakeDetected) => void) => UnlistenFn;
  // Modo teste: enquanto ligado, o disparo do wake NÃO aciona o Live (igual
  // "testar microfone" do Discord). Sempre desligar ao sair da tela.
  wakeSetTestMode: (on: boolean) => Promise<void>;
  // Disparo ocorrido em modo teste (não acionou o Live) — só a tela de teste ouve.
  onWakeTestFire: (callback: (detected: WakeDetected) => void) => UnlistenFn;
  // Wake word com a janela principal VISÍVEL na tela: o Rust manda abrir o Modo
  // Live DENTRO do app (overlay), em vez do chip nativo do notch. Sem payload.
  onWakeLiveInApp: (callback: () => void) => UnlistenFn;
  // Palavra "pare" (papel "stop"): interrompe a fala/o turno no Modo Live.
  onWakeInterrupt: (callback: (detected: WakeDetected) => void) => UnlistenFn;
  // O chip nativo do notch se fechou (X ou auto-desligar por inatividade): uma
  // sessão Live aberta na janela também deve encerrar. Sem payload.
  onLiveCloseRequested: (callback: () => void) => UnlistenFn;
  // Cadastro de frases-gatilho (few-shot pela voz).
  wakePhrases: () => Promise<WakePhrase[]>;
  wakeAddSample: (label: string, samples: number[], role?: 'wake' | 'stop') => Promise<WakePhrase>;
  wakeDeletePhrase: (slug: string) => Promise<void>;
  wakeEnroll: (slug: string) => Promise<WakePhrase>;
  // Gate anti-eco: o webview avisa quando o TTS dele fala, p/ a voz do próprio
  // Neo não disparar o wake word ("Ei Neo") no sidecar.
  wakeTtsState: (speaking: boolean) => Promise<void>;
  // Modo Live aberto DENTRO do app (overlay): a ilha da notch acende as mesmas
  // asas da sessão nativa — microfone de um lado do recorte, stop do outro.
  // Sem isto, o Live in-app deixaria a notch totalmente escondida.
  notchLiveUi: (active: boolean, state?: string, level?: number) => Promise<void>;
  // Sessões anteriores — a fonte é o neo-core (tabela conversation_session);
  // o desktop não mantém índice local. Rejeita se o core estiver inacessível.
  listSessions: (limit?: number) => Promise<SessionInfo[]>;
  /**
   * Decide um card de missão (aprovar / editar / cancelar).
   *
   * Passa pelo Rust porque a chamada precisa do token do aparelho: o `fetch`
   * direto do webview ia sem header e tomava 401 em silêncio desde que as rotas
   * de missão ganharam credencial (neo-core/docs/api-auth-plan.md).
   */
  missionCommand: (
    missionId: string,
    command: 'approve' | 'cancel' | 'edit' | 'retry',
    editedText?: string,
  ) => Promise<unknown>;
  /**
   * Token da ponte da extensão de música (`ws://127.0.0.1:17872`).
   *
   * A ponte passou a exigir credencial: loopback não é fronteira de confiança —
   * qualquer processo da máquina alcança, e uma PÁGINA abre WebSocket para
   * 127.0.0.1. Este token é colado no popup da extensão uma vez.
   */
  extBridgeToken: () => Promise<string>;
  /** Sessão ativa agora (para destacar a linha atual na lista). */
  getSessionId: () => Promise<string>;
  // Caderno de trabalho do Neo (tabela `working_note` no core). Read-only: o
  // visor não mexe em `pinned`/`status`, que mudam o contexto do próprio Neo.
  listWorkingNotes: (limit?: number) => Promise<WorkingNotesPage>;
  readWorkingNote: (slug: string) => Promise<WorkingNoteDetail>;
  /** Abre (ou foca, se já aberta) a janela separada do caderno. */
  openWorkingMemoryWindow: () => Promise<void>;
  // Tarefas em background (tabela `agent_job` no core): lista, detalhe, fim do log
  // e cancelar. Proxy cru do Rust — a forma do JSON é a destes tipos.
  listJobs: (status?: 'active' | 'all', limit?: number) => Promise<JobsPage>;
  getJob: (jobId: string) => Promise<JobDetail>;
  getJobLogs: (jobId: string, tail?: number) => Promise<JobLogs>;
  cancelJob: (jobId: string) => Promise<JobCancelResult>;
  /** Abre (ou foca, se já aberta) a janela de tarefas. */
  openJobsWindow: () => Promise<void>;
}

export interface SessionInfo {
  session_id: string;
  title: string | null;
  message_count: number;
  /** ISO-8601 SEM timezone, gravado pelo core como UTC. */
  last_activity_at: string | null;
}

/** Uma linha da lista do caderno. Sem `body` — o corpo vem por `readWorkingNote`. */
export interface WorkingNoteSummary {
  slug: string;
  title: string;
  summary: string;
  /** `active` ocupa o índice do prompt; `done` é arquivada. */
  status: 'active' | 'done' | string;
  /** `model` = o Neo anotou por conta; `auto` = backstop do turno. */
  origin: 'model' | 'auto' | string;
  pinned: boolean;
  entry_count: number;
  links: string[];
  /** ISO-8601 SEM timezone, gravado pelo core como UTC (igual ao SessionInfo). */
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  session_id: string | null;
}

export interface WorkingNoteDetail extends WorkingNoteSummary {
  /** Markdown inteiro, sem a truncagem que o `wm_read` aplica para o modelo. */
  body: string;
}

/** Uma tarefa em background (linha de `agent_job` no core). */
export interface JobSummary {
  id: string;
  kind: string;
  title: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'timeout' | 'cancelled' | 'waiting' | 'continued' | string;
  notify_policy: string;
  session_id: string | null;
  /** ISO-8601 SEM timezone, gravado pelo core como UTC (igual ao SessionInfo). */
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** Último sinal de vida de um comando que virou job. */
  heartbeat_at: string | null;
  duration_s: number | null;
  exit_code: number | null;
  /** O que foi pedido (comando, tarefa, tópico); encurtado na lista. */
  detail: string;
  preview: string;
  /** Nasceu como `neo_bash` em primeiro plano e virou job por demorar. */
  adopted: boolean;
  /** Tem continuação combinada: ao fim, o Neo termina a entrega sozinho. */
  has_then: boolean;
  continuation_of: string | null;
  has_log: boolean;
}

export interface JobDetail extends JobSummary {
  result_summary: string;
  progress_note: string;
  /** O que o Neo combinou fazer quando a tarefa terminar. */
  then: string;
  log_path: string | null;
  spec: Record<string, unknown>;
}

export interface JobsPage {
  jobs: JobSummary[];
  count: number;
  /** status → quantidade, sobre TODAS as tarefas do dono (não só a página). */
  counts: Record<string, number>;
  active: number;
}

export interface JobLogs {
  job_id: string;
  status: string;
  logs: string;
  /** Havia mais antes do trecho devolvido (o log vem pelo FIM). */
  truncated: boolean;
  source: string;
  error?: string;
}

export interface JobCancelResult {
  ok: boolean;
  status?: string;
  error?: string;
}

export interface WorkingNotesPage {
  notes: WorkingNoteSummary[];
  count: number;
  /** `origin/status` → quantidade (ex.: `auto/active`: 36, `model/active`: 3). */
  counts: Record<string, number>;
}

let passwordRequestUnlisten: UnlistenFn | null = null;
let appStartedUnlisten: UnlistenFn | null = null;
let screenCaptureUnlisten: UnlistenFn | null = null;
let quickHelpChunkUnlisten: UnlistenFn | null = null;
let quickHelpDoneUnlisten: UnlistenFn | null = null;
let quickHelpErrorUnlisten: UnlistenFn | null = null;

// ── agent-event fan-out singleton ────────────────────────────────────────────
// ONE native Tauri listener for 'agent-event', fanned out to every React
// subscriber via a shared Set. Registering `listen()` per effect leaked a SECOND
// native listener under React StrictMode's double-mount and Vite HMR/Fast Refresh
// (the async unlisten resolved too late to cancel the first one), so every event —
// including `complete` — was delivered twice and the chat rendered DUPLICATE
// bubbles (two messages with the same Date.now() id). Add/remove on the Set are
// synchronous, so no remount can leak. The bus lives on globalThis so it also
// survives HMR of THIS module: exactly one native listener, ever.
type AgentEventCb = (event: AgentEvent) => void;
interface AgentEventBus {
  subs: Set<AgentEventCb>;
  started: boolean;
}
const _agentBus: AgentEventBus = ((
  globalThis as unknown as { __neoAgentBus?: AgentEventBus }
).__neoAgentBus ??= { subs: new Set<AgentEventCb>(), started: false });

function ensureAgentEventListener(): void {
  if (_agentBus.started) return;
  _agentBus.started = true;
  void listen<AgentEvent>('agent-event', (event) => {
    // Snapshot: a subscriber may (un)subscribe while we dispatch.
    for (const cb of [..._agentBus.subs]) {
      try {
        cb(event.payload);
      } catch (err) {
        console.error('[tauriApi] agent-event subscriber threw:', err);
      }
    }
  });
}

export const electronAPI: ElectronAPI = {
  sendMessage: (message: string, model?: string, mode?: string) =>
    invoke('execute_with_loop', { message, model, mode, provider: undefined }),

  executeWithLoop: (
    message: string,
    model?: string,
    mode?: string,
    provider?: string,
    attachments?: string[],
    voice?: VoiceInputSource,
    pastedChars?: number
  ) =>
    invoke('execute_with_loop', {
      message,
      model,
      mode,
      provider,
      attachments,
      inputSource: voice?.source,
      asrRaw: voice?.raw,
      asrEngine: voice?.engine,
      pastedChars: pastedChars && pastedChars > 0 ? pastedChars : undefined,
    }),

  pickAttachmentFiles: () => invoke<string[]>('pick_attachment_files'),

  uploadAttachment: (path: string) => invoke<StoredFileMeta>('upload_attachment', { path }),

  downloadGeneratedFile: (fileId: string, fileName: string) =>
    invoke<string>('download_generated_file', { fileId, fileName }),

  loadChatImage: (source: string) => invoke<ChatImage>('load_chat_image', { source }),

  revealLocalFile: (path: string) => invoke<void>('reveal_local_file', { path }),

  // Card de arquivo (services/file_ops.rs): aprovar/rejeitar fora das pastas
  // liberadas, e Desfazer uma alteração já gravada.
  respondFileChange: (changeId: string, approved: boolean) =>
    invoke<void>('respond_file_change', { changeId, approved }),

  undoFileChange: (changeId: string) => invoke<void>('undo_file_change', { changeId }),

  stopExecution: () => invoke('stop_execution'),

  resetAgent: (newSessionId: string) => invoke('reset_agent', { newSessionId }),

  onAgentEvent: (callback: (event: AgentEvent) => void) => {
    // Join the single shared native listener (see ensureAgentEventListener).
    // Subscribe/unsubscribe are synchronous, so StrictMode double-mount and HMR
    // can't leak a duplicate → this is what stopped the doubled chat bubbles.
    ensureAgentEventListener();
    _agentBus.subs.add(callback);
    return () => {
      _agentBus.subs.delete(callback);
    };
  },

  executeCommand: (command: string) => invoke('execute_command', { command }),

  onCommandRequest: (callback: (command: string, explanation: string) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<{ cmd: string; explanation: string }>('command-request', (event) => {
      callback(event.payload.cmd, event.payload.explanation);
    }).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  respondToPermission: (requestId: string, approved: boolean) => {
    // Push the approval/rejection into the live agent stream. Without this the
    // run hangs at "Aguardando permissão" until the 300s timeout.
    invoke('respond_to_command', { commandId: requestId, approved }).catch((e) => {
      console.error('[tauriApi] respond_to_command failed:', e);
    });
  },

  onPasswordRequest: (callback: (prompt: string) => void) => {
    if (passwordRequestUnlisten) passwordRequestUnlisten();
    listen<string>('password-request', (event) => {
      callback(event.payload);
    }).then((unlisten) => {
      passwordRequestUnlisten = unlisten;
    });
    return () => {
      if (passwordRequestUnlisten) {
        passwordRequestUnlisten();
        passwordRequestUnlisten = null;
      }
    };
  },

  respondToPassword: (_password: string | null) => {
    // In Tauri this would be handled differently
  },

  getConversationHistory: () => invoke('get_conversation_history'),
  clearHistory: () => invoke('clear_conversation_history'),
  saveConversationHistory: (messages: unknown[]) =>
    invoke('save_conversation_history', { messages }),

  getWindowMode: () => invoke('get_window_mode'),

  getNeoCoreStatus: () => invoke('get_neo_core_status'),

  onAppStarted: (callback: (payload: unknown) => void) => {
    if (appStartedUnlisten) appStartedUnlisten();
    listen<unknown>('app-started', (event) => callback(event.payload)).then((unlisten) => {
      appStartedUnlisten = unlisten;
    });
    return () => {
      if (appStartedUnlisten) {
        appStartedUnlisten();
        appStartedUnlisten = null;
      }
    };
  },

  minimizeWindow: () => {
    emit('minimize-window');
  },

  closeWindow: () => {
    emit('close-window');
  },

  onScreenCapture: (callback: (data: { imageDataUrl: string; question: string }) => void) => {
    if (screenCaptureUnlisten) screenCaptureUnlisten();
    listen<{ imageDataUrl: string; question: string }>('screen-capture', (event) => {
      callback(event.payload);
    }).then((unlisten) => {
      screenCaptureUnlisten = unlisten;
    });
    return () => {
      if (screenCaptureUnlisten) {
        screenCaptureUnlisten();
        screenCaptureUnlisten = null;
      }
    };
  },

  quickHelpAskStream: (question: string) => {
    invoke('quick_help_ask_stream', { question }).catch(console.error);
  },

  onQuickHelpChunk: (cb: (chunk: string) => void) => {
    if (quickHelpChunkUnlisten) quickHelpChunkUnlisten();
    listen<string>('quick-help-chunk', (event) => {
      cb(event.payload);
    }).then((unlisten) => {
      quickHelpChunkUnlisten = unlisten;
    });
    return () => {
      if (quickHelpChunkUnlisten) {
        quickHelpChunkUnlisten();
        quickHelpChunkUnlisten = null;
      }
    };
  },

  onQuickHelpDone: (cb: () => void) => {
    if (quickHelpDoneUnlisten) quickHelpDoneUnlisten();
    listen('quick-help-done', () => cb()).then((unlisten) => {
      quickHelpDoneUnlisten = unlisten;
    });
    return () => {
      if (quickHelpDoneUnlisten) {
        quickHelpDoneUnlisten();
        quickHelpDoneUnlisten = null;
      }
    };
  },

  onQuickHelpError: (cb: (msg: string) => void) => {
    if (quickHelpErrorUnlisten) quickHelpErrorUnlisten();
    listen<string>('quick-help-error', (event) => {
      cb(event.payload);
    }).then((unlisten) => {
      quickHelpErrorUnlisten = unlisten;
    });
    return () => {
      if (quickHelpErrorUnlisten) {
        quickHelpErrorUnlisten();
        quickHelpErrorUnlisten = null;
      }
    };
  },

  quickHelpResize: (expanded: boolean) => {
    emit('quick-help-resize', expanded);
  },

  quickHelpMoveBy: (dx: number, dy: number) => {
    emit('quick-help-move-by', { dx, dy });
  },

  onNotification: (callback: (notification: NeoNotification) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<NeoNotification>('notification', (event) => {
      callback(event.payload);
    }).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  ackNotification: (notificationId: string) => invoke('ack_notification', { notificationId }),

  onNotificationAction: (callback: (event: NotificationActionEvent) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<NotificationActionEvent>('notification-action', (event) => {
      callback(event.payload);
    }).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  listConnectedDevices: () => invoke('list_connected_devices'),
  devicePairingStatus: () => invoke('device_pairing_status'),
  pairDevice: (code: string, displayName?: string) =>
    invoke('pair_device', { code, displayName: displayName ?? null }),
  renameDevice: (targetDeviceId: string, displayName: string | null) =>
    invoke('rename_device', { targetDeviceId, displayName }),
  removeDevice: (targetDeviceId: string) => invoke('remove_device', { targetDeviceId }),
  setNotificationContext: (activeTab: 'chat' | 'devices') =>
    invoke('set_notification_context', { activeTab }),

  getVoiceConfig: () => invoke('get_voice_config'),

  listVoiceEngines: () => invoke('list_voice_engines'),
  voiceEngineHealth: (key: string) => invoke('voice_engine_health', { key }),
  voiceEngineAction: (key: string, action: VoiceEngineAction) =>
    invoke('voice_engine_action', { key, action }),

  audioRoute: () => invoke('audio_route'),
  listAudioInputs: () => invoke('list_audio_inputs'),
  getAudioInput: () => invoke('get_audio_input'),
  setAudioInput: (name: string | null) => invoke('set_audio_input', { name }),

  headlessStats: () => invoke('headless_stats'),
  headlessGetLimits: () => invoke('headless_get_limits'),
  headlessSetLimits: (limits) => invoke('headless_set_limits', limits),
  headlessShutdown: () => invoke('headless_shutdown'),

  wakeStart: () => invoke('wake_start'),
  wakeStop: () => invoke('wake_stop'),
  wakeStatus: () => invoke('wake_status'),

  onWakeDetected: (callback: (detected: WakeDetected) => void) => {
    // Cada assinante tem seu próprio unlisten (igual onAgentEvent) p/ não vazar
    // listeners no StrictMode/double-mount.
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<WakeDetected>('wake-detected', (event) => callback(event.payload)).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  onWakeScore: (callback: (detected: WakeDetected) => void) => {
    // Mesmo padrão por-subscriber do onWakeDetected. Evento frequente (só quando
    // há fala tipo-wake), mas só há listener com a tela de teste aberta.
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<WakeDetected>('wake-score', (event) => callback(event.payload)).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  wakeSetTestMode: (on: boolean) => invoke('wake_set_test_mode', { on }),

  onWakeTestFire: (callback: (detected: WakeDetected) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<WakeDetected>('wake-test-fire', (event) => callback(event.payload)).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  onWakeLiveInApp: (callback: () => void) => {
    // Mesmo padrão de assinatura-por-subscriber do onWakeDetected (sem vazar no
    // StrictMode/double-mount). Evento sem payload.
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen('wake-live-inapp', () => callback()).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  // O chip nativo do notch se fechou (X ou auto-desligar por inatividade) e o
  // Rust reemite `live-close-requested`: uma sessão Live aberta aqui na janela
  // também deve encerrar, senão o mic continua aberto no webview.
  onLiveCloseRequested: (callback: () => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen('live-close-requested', () => callback()).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  onWakeInterrupt: (callback: (detected: WakeDetected) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<WakeDetected>('wake-interrupt', (event) => callback(event.payload)).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  wakePhrases: () => invoke('wake_phrases'),
  wakeAddSample: (label: string, samples: number[], role: 'wake' | 'stop' = 'wake') =>
    invoke('wake_add_sample', { label, samples, role }),
  wakeDeletePhrase: (slug: string) => invoke('wake_delete_phrase', { slug }),
  wakeEnroll: (slug: string) => invoke('wake_enroll', { slug }),
  wakeTtsState: (speaking: boolean) => invoke('wake_tts_state', { speaking }),
  notchLiveUi: (active: boolean, state?: string, level?: number) =>
    invoke('notch_live_ui', { active, state: state ?? null, level: level ?? null }),

  // Sessões anteriores (proxy pro neo-core, ver commands/mod.rs).
  listSessions: (limit?: number) => invoke<SessionInfo[]>('list_sessions', { limit }),
  missionCommand: (missionId, command, editedText) =>
    invoke('mission_command', { missionId, command, editedText }),
  extBridgeToken: () => invoke<string>('ext_bridge_token'),
  getSessionId: () => invoke<string>('get_session_id'),

  // Caderno de trabalho (proxy pro neo-core, ver commands/mod.rs).
  listWorkingNotes: (limit?: number) =>
    invoke<WorkingNotesPage>('list_working_notes', { limit }),
  readWorkingNote: (slug: string) => invoke<WorkingNoteDetail>('read_working_note', { slug }),
  openWorkingMemoryWindow: () => invoke<void>('open_working_memory_window'),

  // Tarefas em background (proxy pro neo-core, ver commands/mod.rs).
  listJobs: (status?: 'active' | 'all', limit?: number) =>
    invoke<JobsPage>('list_jobs', { status, limit }),
  getJob: (jobId: string) => invoke<JobDetail>('get_job', { jobId }),
  getJobLogs: (jobId: string, tail?: number) => invoke<JobLogs>('get_job_logs', { jobId, tail }),
  cancelJob: (jobId: string) => invoke<JobCancelResult>('cancel_job', { jobId }),
  openJobsWindow: () => invoke<void>('open_jobs_window'),
};
