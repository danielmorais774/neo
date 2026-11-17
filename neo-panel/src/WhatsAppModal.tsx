import { useEffect, useState, useCallback, useRef } from 'react'
import QRCode from 'qrcode'
import { currentToken } from './lib/auth'

const WS_URL = 'ws://127.0.0.1:8000/api/v1/whatsapp/ws/qr-status'
const REST_URL = 'http://127.0.0.1:8000/api/v1/whatsapp'

interface QrStatus {
  qr: string | null
  state: 'disconnected' | 'connecting' | 'connected' | 'unreachable' | 'unknown'
  authReady?: boolean
  error?: string
}

interface WhatsAppModalProps {
  onClose: () => void
}

export default function WhatsAppModal({ onClose }: WhatsAppModalProps) {
  const [qrStatus, setQrStatus] = useState<QrStatus>({ qr: null, state: 'unknown' })
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [clearing, setClearing] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const wsConnectedRef = useRef(false)

  useEffect(() => {
    let mounted = true

    fetch(`${REST_URL}/qr-status`)
      .then(r => r.json())
      .then(data => { if (mounted) setQrStatus(data) })
      .catch(() => { if (mounted) setQrStatus({ qr: null, state: 'unreachable' }) })

    const connect = () => {
      if (!mounted) return
      const ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(currentToken() ?? '')}`)

      ws.onopen = () => {
        wsConnectedRef.current = true
      }

      ws.onmessage = (event) => {
        try {
          const data: QrStatus = JSON.parse(event.data)
          setQrStatus(data)
        } catch { /* ignore */ }
      }

      ws.onclose = () => {
        wsConnectedRef.current = false
        if (mounted) {
          setTimeout(connect, 3000)
        }
      }

      ws.onerror = () => { ws.close() }
      wsRef.current = ws
    }

    connect()

    return () => {
      mounted = false
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
      }
    }
  }, [])

  useEffect(() => {
    if (!qrStatus.qr) { setQrDataUrl(null); return }
    if (qrStatus.qr.startsWith('data:image/png;base64')) {
      setQrDataUrl(qrStatus.qr)
      return
    }
    QRCode.toDataURL(qrStatus.qr, {
      width: 280, margin: 2,
      color: { dark: '#000000', light: '#ffffff' },
    }).then(setQrDataUrl).catch(() => setQrDataUrl(null))
  }, [qrStatus.qr])

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    setQrDataUrl(null)
    try { await fetch(`${REST_URL}/qr-refresh`, { method: 'POST' }) } catch { /* ignore */ }
    setRefreshing(false)
  }, [])

  const handleDisconnect = useCallback(async () => {
    if (!confirm('Isso vai desconectar a sessão do WhatsApp. Continuar?')) return
    setClearing(true)
    try { await fetch(`${REST_URL}/qr-refresh`, { method: 'POST' }) } catch { /* ignore */ }
    setClearing(false)
  }, [])

  const stateColors: Record<string, string> = {
    connected: '#34d399',
    connecting: '#fbbf24',
    disconnected: '#f87171',
    unreachable: '#f87171',
    unknown: '#64748b',
  }

  const stateLabels: Record<string, string> = {
    connected: 'Conectado',
    connecting: 'Aguardando scan…',
    disconnected: 'Desconectado',
    unreachable: 'neo-whatsapp offline',
    unknown: 'Verificando…',
  }

  const state = qrStatus.state
  const color = stateColors[state] ?? '#64748b'

  return (
    <div className="neo-modal-backdrop" onClick={onClose}>
      <div className="neo-modal" onClick={e => e.stopPropagation()} style={{ width: 420, maxWidth: '95vw' }}>
        <div className="neo-modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 36, height: 36, borderRadius: 10,
              background: 'rgba(37, 211, 102, 0.15)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 18,
            }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#25d366" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
              </svg>
            </div>
            <div>
              <div style={{ fontSize: 15, fontWeight: 600, color: '#f1f0f6' }}>WhatsApp</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }}>
                <span style={{
                  width: 7, height: 7, borderRadius: '50%', background: color,
                  boxShadow: `0 0 6px ${color}88`,
                }} />
                <span style={{ fontSize: 11, color }}>{stateLabels[state]}</span>
              </div>
            </div>
          </div>
          <button className="neo-modal-close" onClick={onClose}>×</button>
        </div>

        <div className="neo-modal-body" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 20, padding: '24px 32px' }}>
          {qrDataUrl ? (
            <>
              <div style={{ fontSize: 13, color: '#64748b', textAlign: 'center' }}>
                Escaneie com o WhatsApp
              </div>
              <img
                src={qrDataUrl}
                alt="QR Code"
                style={{ width: 260, height: 260, border: '3px solid #fff', borderRadius: 8 }}
              />
              <div style={{ fontSize: 11, color: '#475569', textAlign: 'center' }}>
                QR code expira em ~20 segundos — escaneie imediatamente
              </div>
            </>
          ) : state === 'connected' ? (
            <div style={{ textAlign: 'center', padding: '16px 0' }}>
              <div style={{
                width: 64, height: 64, borderRadius: '50%',
                background: 'rgba(34, 197, 94, 0.12)',
                border: '2px solid rgba(34, 197, 94, 0.3)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                margin: '0 auto 14px',
              }}>
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#34d399" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12"/>
                </svg>
              </div>
              <div style={{ fontSize: 14, color: '#34d399', fontWeight: 600 }}>
                Sessão ativa e autenticada
              </div>
              <div style={{ fontSize: 12, color: '#475569', marginTop: 4 }}>
                Persistida no volume Docker
              </div>
            </div>
          ) : state === 'connecting' ? (
            <div style={{ textAlign: 'center', padding: '20px 0' }}>
              <div style={{
                width: 48, height: 48, border: '3px solid #334155',
                borderTopColor: '#fbbf24', borderRadius: '50%',
                animation: 'spin 0.8s linear infinite', margin: '0 auto 14px',
              }} />
              <div style={{ fontSize: 14, color: '#fbbf24' }}>
                Aguardando escaneamento…
              </div>
            </div>
          ) : (
            <div style={{ textAlign: 'center', padding: '20px 0' }}>
              <div style={{
                width: 64, height: 64, borderRadius: '50%',
                background: 'rgba(248, 113, 113, 0.12)',
                border: '2px solid rgba(248, 113, 113, 0.3)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                margin: '0 auto 14px',
              }}>
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#f87171" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>
                </svg>
              </div>
              <div style={{ fontSize: 14, color: '#f87171' }}>
                {qrStatus.error ?? 'neo-whatsapp não disponível'}
              </div>
            </div>
          )}
        </div>

        <div className="neo-modal-footer" style={{ justifyContent: 'center', gap: 10 }}>
          {state === 'connected' ? (
            <button
              onClick={handleDisconnect}
              disabled={clearing}
              className="neo-btn-danger"
            >
              {clearing ? 'Desconectando…' : 'Desconectar'}
            </button>
          ) : (
            <button
              onClick={handleRefresh}
              disabled={refreshing || state === 'connecting'}
              className="neo-btn-primary"
            >
              {refreshing ? 'Gerando…' : state === 'connecting' ? 'Aguardando…' : 'Gerar QR Code'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
