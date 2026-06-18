import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const NEO_CORE_HOST = '127.0.0.1:8000'
const PANEL_JWT =
  '***REMOVED***'

const originalFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const request = new Request(input, init)
  const requestUrl = new URL(request.url)
  const isNeoCoreTarget =
    request.url.includes(NEO_CORE_HOST) ||
    requestUrl.pathname.startsWith('/api/')
  if (!isNeoCoreTarget) return originalFetch(request)

  const headers = new Headers(request.headers)
  if (!headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${PANEL_JWT}`)
  }
  return originalFetch(new Request(request, { headers }))
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
