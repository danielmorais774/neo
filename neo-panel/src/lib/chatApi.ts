// Cliente do chat direto com o Neo (neo-core /api/v1/desktop/*) — a versão web
// do que o neo-desktop-tauri faz por gRPC. Não existe grpc-web configurado, então
// aqui a leitura de eventos (thinking/plan/comando/final) vem do endpoint NDJSON
// `/chat/stream` (ver neo-core/api/routes/desktop.py, `_stream_ai_events`).
//
// tool_request (execução de mídia/shell no lado do cliente) não é suportado: só
// o neo-desktop-tauri tem acesso ao SO local para rodar isso.

export { CORE_BASE } from '../lib/auth';
import { CORE_BASE, currentUserId } from '../lib/auth';

// O `user_id` saiu daqui. Era um UUID fixo no fonte, e o core o aceitava como
// verdade: qualquer pedido podia se dizer dono da memória de qualquer um
// (neo-core/docs/api-auth-plan.md §1.1). Agora quem manda é a credencial, e o
// campo só viaja quando há usuário logado — para o core casar e recusar se
// divergir, em vez de aceitar em silêncio.

export interface StoredFileMeta {
  id: string;
  name: string;
  mime: string | null;
  size_bytes: number;
  retention: string;
  expires_at: string | null;
}

export interface AgentStreamEvent {
  type: string;
  data: any;
}

async function streamNdjson(
  url: string,
  body: unknown,
  onEvent: (evt: AgentStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    throw new Error(`Falha ao falar com o Neo (${res.status}): ${text || res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const consumeLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      onEvent(JSON.parse(trimmed));
    } catch (err) {
      console.error('[chatApi] linha NDJSON inválida:', trimmed, err);
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newlineIdx: number;
    while ((newlineIdx = buffer.indexOf('\n')) >= 0) {
      consumeLine(buffer.slice(0, newlineIdx));
      buffer = buffer.slice(newlineIdx + 1);
    }
  }
  consumeLine(buffer);
}

export function streamChat(
  params: {
    sessionId: string | null;
    message: string;
    provider?: string;
    model?: string;
    attachments?: string[];
  },
  onEvent: (evt: AgentStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamNdjson(
    `${CORE_BASE}/api/v1/desktop/chat/stream`,
    {
      type: 'user_input',
      user_message: params.message,
      session_id: params.sessionId,
      user_id: currentUserId() ?? undefined,
      provider: params.provider,
      model: params.model,
      attachments: params.attachments?.length ? params.attachments : undefined,
    },
    onEvent,
    signal,
  );
}

export function streamCommandResponse(
  params: { sessionId: string; commandId: string; command: string; approved: boolean },
  onEvent: (evt: AgentStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamNdjson(
    `${CORE_BASE}/api/v1/desktop/chat/command-response`,
    {
      session_id: params.sessionId,
      command_id: params.commandId,
      command: params.command,
      approved: params.approved,
    },
    onEvent,
    signal,
  );
}

export async function loadSessionHistory(sessionId: string): Promise<any[]> {
  const res = await fetch(`${CORE_BASE}/api/v1/desktop/session/${sessionId}/history`);
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data.messages) ? data.messages : [];
}

export async function uploadAttachment(file: File, sessionId: string): Promise<StoredFileMeta> {
  const form = new FormData();
  form.append('file', file, file.name);
  form.append('retention', 'temp');
  form.append('origin', 'upload-panel');
  form.append('session_id', sessionId);
  const dono = currentUserId();
  if (dono) form.append('user_id', dono);

  const res = await fetch(`${CORE_BASE}/api/v1/files`, { method: 'POST', body: form });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    let friendly = `${res.status} ${body}`;
    try {
      const parsed = JSON.parse(body);
      friendly = parsed?.detail?.message || friendly;
    } catch {
      /* corpo não é JSON — mantém o texto cru */
    }
    throw new Error(friendly);
  }
  return res.json();
}

export async function postMissionCommand(
  missionId: string,
  command: 'approve' | 'cancel' | 'edit' | 'retry',
  editedText?: string,
): Promise<any> {
  const res = await fetch(`${CORE_BASE}/api/v1/missions/${missionId}/command`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(editedText ? { command, edited_text: editedText } : { command }),
  });
  const data = await res.json().catch(() => ({}));
  fetch(`${CORE_BASE}/api/v1/missions/${missionId}/notifications/ack`, { method: 'POST' }).catch(() => {});
  return data;
}
