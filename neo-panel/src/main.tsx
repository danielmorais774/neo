import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import {
  CORE_BASE,
  clearSession,
  currentToken,
  ensureFreshToken,
} from './lib/auth'

const NEO_CORE_HOST = CORE_BASE.replace(/^https?:\/\//, '')

// Toda chamada ao neo-core leva a credencial do usuário logado.
//
// O que estava aqui era uma CONSTANTE: um JWT estático, commitado, sem prazo e
// sem dono (`sub: "neo-panel"`). Agora o token vem do login
// (`lib/auth.ts`), é renovado antes de vencer, e um 401 encerra a sessão em vez
// de virar tela quebrada sem explicação.
// Ver neo-core/docs/api-auth-plan.md.
const originalFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const request = new Request(input, init)
  const requestUrl = new URL(request.url)
  const isNeoCoreTarget =
    request.url.includes(NEO_CORE_HOST) ||
    requestUrl.pathname.startsWith('/api/')
  if (!isNeoCoreTarget) return originalFetch(request)

  // As rotas de login não levam (e não podem levar) credencial.
  if (requestUrl.pathname.startsWith('/api/v1/auth/')) return originalFetch(request)

  await ensureFreshToken()

  const headers = new Headers(request.headers)
  const token = currentToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await originalFetch(new Request(request, { headers }))
  if (response.status === 401) {
    // O refresh já foi tentado acima; 401 aqui é sessão morta de verdade.
    clearSession()
  }
  return response
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
