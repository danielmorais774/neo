import { useEffect, useState } from 'react'
import WhatsAppModal from './WhatsAppModal'

const REST_URL = 'http://127.0.0.1:8000/api/v1/whatsapp'

interface QrStatus {
  qr: string | null
  state: 'disconnected' | 'connecting' | 'connected' | 'unreachable' | 'unknown'
  authReady?: boolean
  error?: string
}

interface SessionDef {
  id: string
  name: string
  icon: React.ReactNode
  color: string
  status?: QrStatus['state']
  description: string
}

const WHATSAPP_ICON = (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
  </svg>
)

const TELEGRAM_ICON = (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
  </svg>
)

const INSTAGRAM_ICON = (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z"/><line x1="17.5" y1="6.5" x2="17.51" y2="6.5"/>
  </svg>
)

const DISCORD_ICON = (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>
  </svg>
)

const STATUS_COLORS: Record<string, string> = {
  connected: '#34d399',
  connecting: '#fbbf24',
  disconnected: '#fb7185',
  unreachable: '#fb7185',
  unknown: '#475569',
}

const STATUS_LABELS: Record<string, string> = {
  connected: 'Conectado',
  connecting: 'Conectando…',
  disconnected: 'Desconectado',
  unreachable: 'Offline',
  unknown: 'Verificando…',
}

export default function SessionsTab() {
  const [whatsappStatus, setWhatsappStatus] = useState<QrStatus['state']>('unknown')
  const [showWhatsAppModal, setShowWhatsAppModal] = useState(false)

  useEffect(() => {
    let mounted = true

    const fetchStatus = () => {
      fetch(`${REST_URL}/qr-status`)
        .then(r => r.json())
        .then((data: QrStatus) => { if (mounted) setWhatsappStatus(data.state) })
        .catch(() => { if (mounted) setWhatsappStatus('unreachable') })
    }

    fetchStatus()
    const interval = setInterval(fetchStatus, 15000)
    return () => { mounted = false; clearInterval(interval) }
  }, [])

  const sessions: SessionDef[] = [
    {
      id: 'whatsapp',
      name: 'WhatsApp',
      icon: WHATSAPP_ICON,
      color: '#25d366',
      status: whatsappStatus,
      description: 'Mensagens e chamadas via Baileys',
    },
    {
      id: 'telegram',
      name: 'Telegram',
      icon: TELEGRAM_ICON,
      color: '#0088cc',
      description: 'Em breve',
    },
    {
      id: 'instagram',
      name: 'Instagram',
      icon: INSTAGRAM_ICON,
      color: '#e4405f',
      description: 'Em breve',
    },
    {
      id: 'discord',
      name: 'Discord',
      icon: DISCORD_ICON,
      color: '#5865f2',
      description: 'Em breve',
    },
  ]

  return (
    <>
      <div className="neo-content" style={{ maxWidth: 720 }}>
        <div style={{ marginBottom: 32 }}>
          <h2 className="neo-h2">Sessões</h2>
          <p className="neo-desc">
            Gerencie as conexões com redes sociais e serviços de mensageria.
            Clique em uma sessão para configurar ou verificar o status.
          </p>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {sessions.map(session => {
            const isAvailable = session.status !== undefined
            const statusColor = session.status ? STATUS_COLORS[session.status] : '#475569'
            const statusLabel = session.status ? STATUS_LABELS[session.status] : session.description

            return (
              <div
                key={session.id}
                onClick={() => {
                  if (session.id === 'whatsapp') setShowWhatsAppModal(true)
                }}
                className="neo-card"
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 16,
                  padding: '18px 22px',
                  cursor: isAvailable ? 'pointer' : 'default',
                  opacity: isAvailable ? 1 : 0.5,
                  transition: 'border-color 0.15s, box-shadow 0.15s',
                }}
                onMouseEnter={e => {
                  if (isAvailable) {
                    e.currentTarget.style.borderColor = `${session.color}44`
                    e.currentTarget.style.boxShadow = `0 0 20px ${session.color}15`
                  }
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.borderColor = ''
                  e.currentTarget.style.boxShadow = ''
                }}
              >
                <div style={{
                  width: 48, height: 48, borderRadius: 12,
                  background: `${session.color}18`,
                  border: `1px solid ${session.color}30`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: session.color, flexShrink: 0,
                }}>
                  {session.icon}
                </div>

                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ fontSize: 14, fontWeight: 600, color: '#f1f0f6' }}>
                      {session.name}
                    </span>
                    {isAvailable && (
                      <span style={{
                        display: 'inline-flex', alignItems: 'center', gap: 5,
                        fontSize: 11, padding: '2px 8px', borderRadius: 8,
                        background: `${statusColor}15`,
                        border: `1px solid ${statusColor}30`,
                        color: statusColor,
                      }}>
                        <span style={{
                          width: 6, height: 6, borderRadius: '50%',
                          background: statusColor,
                          boxShadow: session.status === 'connected' ? `0 0 6px ${statusColor}88` : 'none',
                        }} />
                        {statusLabel}
                      </span>
                    )}
                    {!isAvailable && (
                      <span style={{
                        fontSize: 11, padding: '2px 8px', borderRadius: 8,
                        background: 'rgba(100, 116, 139, 0.1)',
                        border: '1px solid rgba(100, 116, 139, 0.2)',
                        color: '#64748b',
                      }}>
                        {session.description}
                      </span>
                    )}
                  </div>
                  {!isAvailable && (
                    <div style={{ fontSize: 12, color: '#475569', marginTop: 4 }}>
                      {session.description}
                    </div>
                  )}
                </div>

                {isAvailable && (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                    <polyline points="9 18 15 12 9 6"/>
                  </svg>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {showWhatsAppModal && (
        <WhatsAppModal onClose={() => setShowWhatsAppModal(false)} />
      )}
    </>
  )
}
