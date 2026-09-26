// Credencial do painel (neo-core/docs/api-auth-plan.md §5).
//
// **O que saiu daqui.** O painel usava um JWT ESTÁTICO commitado no fonte, com
// `sub: "neo-panel"`, sem prazo, assinado com o segredo default do
// `shared/config.py`. Quem lia o repositório emitia token de admin — e, por não
// dizer QUEM era, esse token nunca pôde aprovar liberação de dado. Agora existe
// login de verdade e o token é do usuário.
//
// O token fica em `sessionStorage`, não em `localStorage`: fechar a aba encerra a
// sessão, e o painel abre no mesmo navegador onde ele lê e-mail. O refresh é
// opaco e rotaciona a cada renovação (o core queima o anterior).
//
// `VITE_PANEL_TOKEN` é escotilha de desenvolvimento — um token estático que você
// gera e passa por ambiente. Fora de `strict`, o core ainda o aceita.

const STORAGE_KEY = 'neo.auth.session';
export const CORE_BASE = 'http://127.0.0.1:8000';
export const LEGACY_PANEL_TOKEN: string =
  (import.meta as any).env?.VITE_PANEL_TOKEN ?? '';

export interface AuthUser {
  id: string;
  username: string | null;
  display_name: string | null;
}

export interface AuthSession {
  accessToken: string;
  refreshToken: string;
  /** Epoch em SEGUNDOS, como vem do core (`exp` do JWT). */
  expiresAt: number;
  user: AuthUser;
}

export interface AuthMode {
  mode: 'off' | 'shadow' | 'enforce' | 'strict';
  login_required: boolean;
  legacy_panel_token_accepted: boolean;
}

type Listener = () => void;
const listeners = new Set<Listener>();

function load(): AuthSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as AuthSession) : null;
  } catch {
    return null;
  }
}

let session: AuthSession | null = load();

function save(next: AuthSession | null) {
  session = next;
  try {
    if (next) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Navegador sem storage (janela privada): a sessão vive só em memória.
  }
  listeners.forEach((fn) => fn());
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function currentSession(): AuthSession | null {
  return session;
}

export function currentUser(): AuthUser | null {
  return session?.user ?? null;
}

/** O `user_id` do dono logado. As telas que mandavam um UUID fixo usam isto. */
export function currentUserId(): string | null {
  return session?.user.id ?? null;
}

export function isAuthenticated(): boolean {
  return session !== null;
}

/** O token que vai no header, ou o legado quando não há sessão. */
export function currentToken(): string | null {
  return session?.accessToken ?? (LEGACY_PANEL_TOKEN || null);
}

export async function fetchAuthMode(): Promise<AuthMode> {
  const res = await fetch(`${CORE_BASE}/api/v1/auth/mode`);
  if (!res.ok) throw new Error(`neo-core respondeu ${res.status}`);
  return (await res.json()) as AuthMode;
}

export async function login(username: string, password: string): Promise<AuthSession> {
  const res = await fetch(`${CORE_BASE}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const corpo = await res.json().catch(() => ({}));
    throw new Error(
      res.status === 429
        ? (corpo.detail ?? 'tentativas demais, espere um pouco')
        : (corpo.detail ?? 'usuário ou senha inválidos'),
    );
  }
  const corpo = await res.json();
  const nova: AuthSession = {
    accessToken: corpo.access_token,
    refreshToken: corpo.refresh_token,
    expiresAt: corpo.expires_at,
    user: corpo.user,
  };
  save(nova);
  return nova;
}

export async function logout(): Promise<void> {
  const atual = session;
  save(null);
  if (!atual) return;
  // Revogar é melhor-esforço: o token local já foi apagado de qualquer forma.
  try {
    await fetch(`${CORE_BASE}/api/v1/auth/logout`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: atual.refreshToken }),
    });
  } catch {
    /* rede caiu — o refresh expira sozinho */
  }
}

/** Marca a sessão como perdida (401 que o refresh não resolveu). */
export function clearSession(): void {
  save(null);
}

let renovando: Promise<void> | null = null;

/**
 * Renova o access token quando falta menos de um minuto.
 *
 * Uma renovação por vez (`renovando`): o painel dispara várias chamadas em
 * paralelo ao abrir uma tela, e duas renovações simultâneas consumiriam o mesmo
 * refresh — a segunda tomaria 401 e derrubaria a sessão inteira.
 */
export async function ensureFreshToken(): Promise<void> {
  if (!session) return;
  const faltam = session.expiresAt - Math.floor(Date.now() / 1000);
  if (faltam > 60) return;
  if (renovando) return renovando;

  const refreshToken = session.refreshToken;
  renovando = (async () => {
    try {
      const res = await fetch(`${CORE_BASE}/api/v1/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) {
        save(null);
        return;
      }
      const corpo = await res.json();
      save({
        accessToken: corpo.access_token,
        refreshToken: corpo.refresh_token,
        expiresAt: corpo.expires_at,
        user: corpo.user,
      });
    } catch {
      // Falha de rede não apaga a sessão: o token atual pode ainda valer.
    } finally {
      renovando = null;
    }
  })();
  return renovando;
}
