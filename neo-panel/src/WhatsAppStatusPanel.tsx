import { useEffect, useState, useCallback, useRef } from 'react'
import QRCode from 'qrcode'

const WS_URL = 'ws://localhost:8000/api/v1/whatsapp/ws/qr-status'
const REST_URL = 'http://localhost:8000/api/v1/whatsapp'

interface QrStatus {
  qr: string | null
  state: 'disconnected' | 'connecting' | 'connected' | 'unreachable' | 'unknown'
  authReady?: boolean
  error?: string
}

export default function WhatsAppStatus() {
  const [qrStatus, setQrStatus] = useState<QrStatus>({ qr: null, state: 'unknown' })
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [clearing, setClearing] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const wsConnectedRef = useRef(false)

  // Connect to WebSocket for real-time updates
  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(WS_URL)

      ws.onopen = () => {
        wsConnectedRef.current = true
        console.log('[WS] QR status connected')
      }

      ws.onmessage = (event) => {
        try {
          const data: QrStatus = JSON.parse(event.data)
          setQrStatus(data)
        } catch {
          // ignore parse errors
        }
      }

      ws.onclose = () => {
        wsConnectedRef.current = false
        console.log('[WS] QR status disconnected, reconnecting in 3s')
        setTimeout(connect, 3000)
      }

      ws.onerror = () => {
        ws.close()
      }

      wsRef.current = ws
    }

    connect()

    return () => {
      if (wsRef.current) {
        wsRef.current.onclose = null // prevent reconnect on intentional unmount
        wsRef.current.close()
      }
    }
  }, [])

  // Render QR code as data URL when we have one
  useEffect(() => {
    if (!qrStatus.qr) {
      setQrDataUrl(null)
      return
    }
    // neo-whatsapp already sends a PNG data URL — use it directly
    if (qrStatus.qr.startsWith('data:image/png;base64')) {
      setQrDataUrl(qrStatus.qr)
      return
    }
    // Baileys-style comma-separated string — render with qrcode
    QRCode.toDataURL(qrStatus.qr, {
      width: 280,
      margin: 2,
      color: { dark: '#000000', light: '#ffffff' },
    }).then(setQrDataUrl).catch(() => setQrDataUrl(null))
  }, [qrStatus.qr])

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    setQrDataUrl(null)
    try {
      // POST to qr-refresh triggers broadcast to all WS subscribers
      await fetch(`${REST_URL}/qr-refresh`, { method: 'POST' })
      // WS will deliver the updated status
    } catch { /* ignore */ }
    setRefreshing(false)
  }, [])

  const handleClearSession = useCallback(async () => {
    if (!confirm('Isso vai remover a sessão do WhatsApp e gerar um novo QR code. Continuar?')) return
    setClearing(true)
    try {
      await fetch(`${REST_URL}/qr-refresh`, { method: 'POST' })
    } catch { /* ignore */ }
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
    connected: 'Conectado ao WhatsApp',
    connecting: 'Aguardando scan do QR code…',
    disconnected: 'Desconectado',
    unreachable: 'neo-whatsapp offline',
    unknown: 'Verificando…',
  }

  const state = qrStatus.state
  const color = stateColors[state] ?? '#64748b'

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      justifyContent: 'center', flex: 1, gap: 24, padding: 40,
      background: '#0f1117', color: '#e2e8f0', minHeight: 0,
    }}>
      {/* Title */}
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 28, fontWeight: 700, color: '#7c3aed', marginBottom: 4 }}>
          📱 WhatsApp
        </div>
        <div style={{ fontSize: 13, color: '#475569' }}>
          neo-whatsapp — autenticação persistente via Docker volume
        </div>
      </div>

      {/* Status card */}
      <div style={{
        background: '#13161f', border: `1px solid #1e2235`,
        borderRadius: 16, padding: '24px 32px', textAlign: 'center',
        minWidth: 320, maxWidth: 400, width: '100%',
      }}>
        {/* Status dot + label */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, marginBottom: 20 }}>
          <div style={{
            width: 14, height: 14, borderRadius: '50%', background: color,
            boxShadow: `0 0 8px ${color}88`,
          }} />
          <span style={{ fontSize: 16, fontWeight: 600, color }}>{stateLabels[state]}</span>
        </div>

        {/* QR Code */}
        {qrDataUrl ? (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 12 }}>
              Escaneie com o WhatsApp
            </div>
            <img
              src={qrDataUrl}
              alt="QR Code"
              style={{ width: 280, height: 280, border: '4px solid #fff', borderRadius: 8 }}
            />
            <div style={{ fontSize: 11, color: '#334155', marginTop: 8 }}>
              QR code expira em ~20 segundos — escaneie imediatamente
            </div>
          </div>
        ) : state === 'connected' ? (
          <div style={{ padding: '20px 0' }}>
            <div style={{ fontSize: 48 }}>✅</div>
            <div style={{ fontSize: 14, color: '#34d399', marginTop: 8 }}>
              Sessão ativa e autenticada
            </div>
            <div style={{ fontSize: 12, color: '#475569', marginTop: 4 }}>
              A sessão é persistida no volume Docker e sobrevive a reinícios
            </div>
          </div>
        ) : state === 'connecting' ? (
          <div style={{ padding: '20px 0' }}>
            <div style={{ fontSize: 48 }}>⏳</div>
            <div style={{ fontSize: 14, color: '#fbbf24', marginTop: 8 }}>
              Aguardando escaneamento…
            </div>
          </div>
        ) : (
          <div style={{ padding: '20px 0' }}>
            <div style={{ fontSize: 48 }}>❌</div>
            <div style={{ fontSize: 14, color: '#f87171', marginTop: 8 }}>
              {qrStatus.error ?? 'neo-whatsapp não disponível'}
            </div>
          </div>
        )}

        {/* Actions */}
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 16 }}>
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            style={{
              background: '#1e2235', color: '#e2e8f0', border: '1px solid #334155',
              borderRadius: 8, padding: '8px 18px', cursor: 'pointer', fontSize: 13,
              opacity: refreshing ? 0.5 : 1,
            }}
          >
            {refreshing ? 'Gerando…' : '🔄 Gerar novo QR'}
          </button>

          {state === 'connected' && (
            <button
              onClick={handleClearSession}
              disabled={clearing}
              style={{
                background: '#1e2235', color: '#f87171', border: '1px solid #334155',
                borderRadius: 8, padding: '8px 18px', cursor: 'pointer', fontSize: 13,
                opacity: clearing ? 0.5 : 1,
              }}
            >
              {clearing ? 'Limpando…' : '🗑️ Desconectar'}
            </button>
          )}
        </div>
      </div>

      {/* Help text */}
      <div style={{ fontSize: 12, color: '#334155', textAlign: 'center', maxWidth: 360 }}>
        A sessão do WhatsApp é armazenada no volume Docker <code style={{ color: '#475569' }}>baileys-auth</code>.
        O QR code só aparece quando não há sessão salva ou após desconectar.
      </div>
    </div>
  )
}