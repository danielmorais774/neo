import { useEffect, useState, useCallback, useRef } from 'react'

const API = 'http://127.0.0.1:8000/api/v1/whatsapp'

interface Chat {
  jid: string
  name: string
  unread: number
}

interface Message {
  id: string
  from: string
  body: string
  caption?: string | null
  mediaType?: string | null
  mimeType?: string | null
  fileName?: string | null
  timestamp: number
  fromMe: boolean
}

interface SendResult {
  ok: boolean
  message_id?: string
  error?: string
  detail?: string
}

const s = {
  root: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, background: '#0f1117', color: '#e2e8f0' } as React.CSSProperties,
  topbar: { display: 'flex', alignItems: 'center', gap: 12, padding: '10px 20px', borderBottom: '1px solid #1e2235', flexShrink: 0 } as React.CSSProperties,
  title: { color: '#7c3aed', fontWeight: 700, fontSize: 15 } as React.CSSProperties,
  body: { display: 'flex', flex: 1, minHeight: 0, overflow: 'hidden' } as React.CSSProperties,

  sidebar: { width: 300, borderRight: '1px solid #1e2235', display: 'flex', flexDirection: 'column', overflow: 'hidden' } as React.CSSProperties,
  sideHead: { padding: '10px 14px', borderBottom: '1px solid #1e2235', display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 } as React.CSSProperties,
  sideLabel: { fontSize: 12, color: '#7c3aed', fontWeight: 700, letterSpacing: '0.05em' } as React.CSSProperties,
  badge: { background: '#7c3aed', color: '#fff', borderRadius: 10, padding: '2px 8px', fontSize: 11, fontWeight: 700 } as React.CSSProperties,
  searchWrap: { padding: '8px 10px', borderBottom: '1px solid #1e2235', flexShrink: 0 } as React.CSSProperties,
  scroll: { flex: 1, overflowY: 'auto' } as React.CSSProperties,
  empty: { padding: '24px 14px', color: '#475569', fontSize: 13, textAlign: 'center' } as React.CSSProperties,

  chatItem: (active: boolean): React.CSSProperties => ({
    padding: '10px 14px', cursor: 'pointer',
    borderBottom: '1px solid #13161f',
    background: active ? '#1e2235' : 'transparent',
    transition: 'background 0.1s',
  }),

  main: { flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 } as React.CSSProperties,
  placeholder: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#475569', fontSize: 14 } as React.CSSProperties,

  chatHead: { padding: '12px 18px', borderBottom: '1px solid #1e2235', flexShrink: 0 } as React.CSSProperties,
  msgList: { flex: 1, overflowY: 'auto', padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 8 } as React.CSSProperties,

  bubble: (fromMe: boolean): React.CSSProperties => ({
    maxWidth: '68%', padding: '8px 12px', borderRadius: fromMe ? '12px 12px 2px 12px' : '12px 12px 12px 2px',
    background: fromMe ? '#5b21b6' : '#1e2235',
    color: '#e2e8f0', fontSize: 13, lineHeight: 1.45,
    alignSelf: fromMe ? 'flex-end' : 'flex-start',
  }),
  bubbleTime: { fontSize: 10, color: '#64748b', marginTop: 4, textAlign: 'right' } as React.CSSProperties,

  compose: { padding: '14px 18px', borderTop: '1px solid #1e2235', flexShrink: 0 } as React.CSSProperties,
  composeRow: { display: 'flex', gap: 8, marginTop: 8 } as React.CSSProperties,

  input: { flex: 1, background: '#13161f', border: '1px solid #1e2235', borderRadius: 8, padding: '8px 12px', color: '#e2e8f0', fontSize: 14, outline: 'none' } as React.CSSProperties,
  textarea: { width: '100%', background: '#13161f', border: '1px solid #1e2235', borderRadius: 8, padding: '8px 12px', color: '#e2e8f0', fontSize: 14, outline: 'none', resize: 'none', fontFamily: 'inherit', boxSizing: 'border-box' } as React.CSSProperties,

  btn: { background: '#7c3aed', color: '#fff', border: 'none', borderRadius: 8, padding: '8px 18px', cursor: 'pointer', fontWeight: 600, fontSize: 14 } as React.CSSProperties,
  btnSm: { background: '#7c3aed', color: '#fff', border: 'none', borderRadius: 8, padding: '5px 12px', cursor: 'pointer', fontWeight: 600, fontSize: 12 } as React.CSSProperties,
  btnDisabled: { background: '#7c3aed', color: '#fff', border: 'none', borderRadius: 8, padding: '8px 18px', cursor: 'not-allowed', fontWeight: 600, fontSize: 14, opacity: 0.45 } as React.CSSProperties,

  dot: (ok: boolean | null): React.CSSProperties => ({
    width: 8, height: 8, borderRadius: '50%',
    background: ok === true ? '#34d399' : ok === false ? '#f87171' : '#64748b',
  }),
  connLabel: (ok: boolean | null): React.CSSProperties => ({
    fontSize: 12, color: ok === true ? '#34d399' : ok === false ? '#f87171' : '#64748b',
  }),
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' })
}

function formatAudioTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

interface AudioPlayerProps {
  src: string
  fileName?: string | null
}

function AudioPlayer({ src, fileName }: AudioPlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [duration, setDuration] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const animFrame = useRef<number>(0)

  const drawWaveform = useCallback((ctx: CanvasRenderingContext2D, w: number, h: number, prog: number) => {
    ctx.clearRect(0, 0, w, h)
    const bars = 40
    const gap = 2
    const totalGap = gap * (bars - 1)
    const barW = (w - totalGap) / bars
    for (let i = 0; i < bars; i++) {
      const phase = (Date.now() / 300 + i * 0.3) % (Math.PI * 2)
      const wave = playing ? Math.abs(Math.sin(phase)) * 0.5 + 0.5 : 0.3
      const normX = (i * (barW + gap)) / w
      const filled = normX < prog
      const alpha = filled ? 1 : 0.25
      const barH = Math.max(3, wave * (h * 0.9))
      const x = i * (barW + gap)
      const y = (h - barH) / 2
      ctx.fillStyle = filled ? `rgba(124, 58, 237, ${alpha})` : `rgba(100, 116, 139, ${alpha})`
      ctx.beginPath()
      ctx.roundRect(x, y, barW, barH, barW / 2)
      ctx.fill()
    }
  }, [playing])

  const startDraw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const w = canvas.width
    const h = canvas.height
    const prog = duration > 0 ? elapsed / duration : 0
    drawWaveform(ctx, w, h, prog)
    if (playing) {
      animFrame.current = requestAnimationFrame(startDraw)
    }
  }, [playing, elapsed, duration, drawWaveform])

  useEffect(() => {
    if (!audioRef.current) {
      audioRef.current = new Audio()
    }
    const audio = audioRef.current
    audio.src = src
    audio.crossOrigin = 'anonymous'

    const onTime = () => setElapsed(audio.currentTime)
    const onDur = () => setDuration(audio.duration || 0)
    const onEnd = () => { setPlaying(false); setElapsed(0) }
    audio.addEventListener('timeupdate', onTime)
    audio.addEventListener('loadedmetadata', onDur)
    audio.addEventListener('ended', onEnd)
    return () => {
      audio.removeEventListener('timeupdate', onTime)
      audio.removeEventListener('loadedmetadata', onDur)
      audio.removeEventListener('ended', onEnd)
    }
  }, [src])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    if (playing) {
      audio.play().catch(() => setPlaying(false))
      animFrame.current = requestAnimationFrame(startDraw)
    } else {
      audio.pause()
      cancelAnimationFrame(animFrame.current)
    }
    return () => cancelAnimationFrame(animFrame.current)
  }, [playing, startDraw])

  useEffect(() => {
    setPlaying(false)
    setElapsed(0)
    setDuration(0)
  }, [src])

  const togglePlay = () => setPlaying(p => !p)

  const seek = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    const audio = audioRef.current
    if (!canvas || !audio || duration === 0) return
    const rect = canvas.getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    audio.currentTime = ratio * duration
    setElapsed(ratio * duration)
  }

  const progPct = duration > 0 ? (elapsed / duration) * 100 : 0

  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: '#13161f', borderRadius: 10, padding: '8px 12px' }}>
        <button
          onClick={togglePlay}
          style={{
            width: 32, height: 32, borderRadius: '50%', border: 'none',
            background: playing ? '#7c3aed' : 'linear-gradient(135deg, #7c3aed, #5b21b6)',
            boxShadow: playing ? '0 0 12px rgba(124,58,237,0.7)' : '0 0 8px rgba(124,58,237,0.4)',
            cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0, transition: 'all 0.2s',
          }}
        >
          {playing ? (
            <svg width="12" height="14" viewBox="0 0 12 14" fill="white">
              <rect x="0" y="0" width="4" height="14" rx="1"/>
              <rect x="8" y="0" width="4" height="14" rx="1"/>
            </svg>
          ) : (
            <svg width="12" height="14" viewBox="0 0 12 14" fill="white" style={{ marginLeft: 2 }}>
              <path d="M1 1l10 6-10 6z"/>
            </svg>
          )}
        </button>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
            <span style={{ fontSize: 11, color: '#7c3aed', fontWeight: 600 }}>{fileName || 'Audio'}</span>
            <span style={{ fontSize: 10, color: '#64748b', fontVariantNumeric: 'tabular-nums' }}>
              {formatAudioTime(elapsed)} / {formatAudioTime(duration)}
            </span>
          </div>
          <canvas
            ref={canvasRef}
            width={180}
            height={28}
            onClick={seek}
            style={{ width: '100%', height: 28, cursor: 'pointer', borderRadius: 4, display: 'block' }}
          />
          <div style={{
            height: 2, background: '#1e2235', borderRadius: 1, marginTop: 4,
            position: 'relative', overflow: 'hidden',
          }}>
            <div style={{
              position: 'absolute', left: 0, top: 0, height: '100%',
              width: `${progPct}%`,
              background: 'linear-gradient(90deg, #7c3aed, #a78bfa)',
              borderRadius: 1,
              boxShadow: '0 0 6px rgba(124,58,237,0.6)',
              transition: playing ? 'none' : 'width 0.1s linear',
            }}/>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function WhatsAppPanel() {
  const [chats, setChats] = useState<Chat[]>([])
  const [chatsLoading, setChatsLoading] = useState(false)
  const [selected, setSelected] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [msgsLoading, setMsgsLoading] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const msgListRef = useRef<HTMLDivElement>(null)
  const atTopRef = useRef(false)
  const [connOk, setConnOk] = useState<boolean | null>(null)
  const [search, setSearch] = useState('')
  const [composerText, setComposerText] = useState('')
  const [sending, setSending] = useState(false)
  const [statusMsg, setStatusMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [mediaUrls, setMediaUrls] = useState<Record<string, string>>({})
  const [mediaLoading, setMediaLoading] = useState<Record<string, boolean>>({})

  const showStatus = useCallback((type: 'success' | 'error', text: string) => {
    setStatusMsg({ type, text })
    if (statusTimer.current) clearTimeout(statusTimer.current)
    statusTimer.current = setTimeout(() => setStatusMsg(null), 5000)
  }, [])

  const checkConn = useCallback(async () => {
    try {
      const res = await fetch(`${API}/connection-status`)
      const data = await res.json()
      setConnOk(!!data.ok)
    } catch { setConnOk(false) }
  }, [])

  const loadChats = useCallback(async () => {
    setChatsLoading(true)
    try {
      const res = await fetch(`${API}/chats`)
      if (res.ok) {
        const data = await res.json()
        setChats(data.chats ?? [])
      } else {
        setChats([])
      }
    } catch { setChats([]) }
    finally { setChatsLoading(false) }
  }, [])

  const loadMessages = useCallback(async (jid: string, prepend = false) => {
    if (prepend) {
      setLoadingMore(true)
    } else {
      setMsgsLoading(true)
      setHasMore(false)
    }
    try {
      const url = prepend && messages.length > 0
        ? `${API}/chats/${encodeURIComponent(jid)}/messages?before=${encodeURIComponent(messages[0].id)}`
        : `${API}/chats/${encodeURIComponent(jid)}/messages`
      const res = await fetch(url)
      if (res.ok) {
        const data = await res.json()
        setMessages(prev => prepend ? [...data.messages, ...prev] : (data.messages ?? []))
        setHasMore(data.messages?.length >= 100)
      }
    } catch { /* silent */ }
    finally {
      setMsgsLoading(false)
      setLoadingMore(false)
    }
  }, [messages])

  // Scroll to bottom when messages change
  useEffect(() => {
    if (msgListRef.current) {
      msgListRef.current.scrollTop = msgListRef.current.scrollHeight
    }
  }, [messages])

  // Initial load + periodic refresh
  useEffect(() => {
    checkConn()
    loadChats()
    const connInterval = setInterval(checkConn, 15000)
    const chatInterval = setInterval(loadChats, 30000)
    return () => { clearInterval(connInterval); clearInterval(chatInterval) }
  }, [checkConn, loadChats])

  // Auto-refresh: prepend new messages instead of replacing
  useEffect(() => {
    if (!selected) return
    const interval = setInterval(async () => {
      if (messages.length === 0) { loadMessages(selected.jid); return }
      try {
        const res = await fetch(`${API}/chats/${encodeURIComponent(selected.jid)}/messages?limit=20`)
        if (res.ok) {
          const data = await res.json()
          const newest = data.messages?.[data.messages.length - 1]
          if (newest && messages[messages.length - 1]?.id !== newest.id) {
            setMessages(prev => [...prev, ...data.messages.slice(prev.length ? data.messages.indexOf(prev[prev.length-1]) + 1 : 0)])
          }
        }
      } catch {}
    }, 8000)
    return () => clearInterval(interval)
  }, [selected, messages])

  const handleScrollTop = useCallback(() => {
    if (!hasMore || loadingMore || !selected) return
    atTopRef.current = true
    loadMessages(selected.jid, true).then(() => { atTopRef.current = false })
  }, [hasMore, loadingMore, selected, loadMessages])

  const loadMedia = useCallback(async (jid: string, msgId: string) => {
    if (mediaUrls[msgId] || mediaLoading[msgId]) return
    setMediaLoading(prev => ({ ...prev, [msgId]: true }))
    try {
      const res = await fetch(`${API}/chats/${encodeURIComponent(jid)}/media/${encodeURIComponent(msgId)}`)
      if (res.ok) {
        const data = await res.json()
        if (data.url) setMediaUrls(prev => ({ ...prev, [msgId]: data.url }))
      }
    } catch {} finally {
      setMediaLoading(prev => ({ ...prev, [msgId]: false }))
    }
  }, []) // intentionally no deps — always uses current state via closures

  const handleSelectChat = useCallback((chat: Chat) => {
    setSelected(chat)
    setMessages([])
    setComposerText('')
    setStatusMsg(null)
    setMediaUrls({})
    loadMessages(chat.jid)
  }, [loadMessages])

  const handleSend = useCallback(async () => {
    if (!selected || !composerText.trim()) return
    setSending(true)
    setStatusMsg(null)
    try {
      const params = new URLSearchParams({ jid: selected.jid, text: composerText.trim() })
      const res = await fetch(`${API}/send?${params}`, { method: 'POST' })
      const data: SendResult = await res.json()
      if (data.ok) {
        showStatus('success', `Enviado (${data.message_id?.slice(0, 8) ?? ''}…)`)
        setComposerText('')
        await loadMessages(selected.jid)
      } else {
        showStatus('error', `Erro: ${data.error ?? data.detail ?? 'desconhecido'}`)
      }
    } catch (e: unknown) {
      showStatus('error', `Erro de rede: ${e instanceof Error ? e.message : String(e)}`)
    } finally { setSending(false) }
  }, [selected, composerText, showStatus, loadMessages])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleSend() }
  }, [handleSend])

  const filtered = chats.filter(c =>
    c.name.toLowerCase().includes(search.toLowerCase()) ||
    c.jid.toLowerCase().includes(search.toLowerCase())
  )

  const canSend = !!selected && composerText.trim().length > 0 && !sending

  return (
    <div style={s.root}>
      {/* Top bar */}
      <div style={s.topbar}>
        <span style={s.title}>WhatsApp</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 8 }}>
          <div style={s.dot(connOk)} />
          <span style={s.connLabel(connOk)}>
            {connOk === null ? 'Verificando…' : connOk ? 'Conectado' : 'Offline'}
          </span>
        </div>
        <button onClick={() => { checkConn(); loadChats(); if (selected) loadMessages(selected.jid) }} style={{ ...s.btnSm, marginLeft: 'auto' }}>
          Atualizar
        </button>
      </div>

      <div style={s.body}>
        {/* Sidebar: live chats */}
        <div style={s.sidebar}>
          <div style={s.sideHead}>
            <span style={s.sideLabel}>CONVERSAS</span>
            <span style={s.badge}>{chats.length}</span>
            {chatsLoading && <span style={{ fontSize: 11, color: '#475569', marginLeft: 'auto' }}>carregando…</span>}
          </div>
          <div style={s.searchWrap}>
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Buscar conversa…"
              style={{ ...s.input, width: '100%', fontSize: 13, padding: '6px 10px', boxSizing: 'border-box' }}
            />
          </div>
          <div style={s.scroll}>
            {filtered.length === 0 && (
              <div style={s.empty}>
                {chats.length === 0
                  ? connOk ? 'Nenhuma conversa encontrada' : 'WhatsApp offline — verifique a conexão'
                  : 'Nenhum resultado'}
              </div>
            )}
            {filtered.map(chat => (
              <div
                key={chat.jid}
                style={s.chatItem(selected?.jid === chat.jid)}
                onClick={() => handleSelectChat(chat)}
                onMouseEnter={e => { if (selected?.jid !== chat.jid) (e.currentTarget as HTMLDivElement).style.background = '#161a28' }}
                onMouseLeave={e => { if (selected?.jid !== chat.jid) (e.currentTarget as HTMLDivElement).style.background = 'transparent' }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontWeight: 600, fontSize: 14, color: '#e2e8f0', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {chat.name || chat.jid.split('@')[0]}
                  </span>
                  {chat.unread > 0 && (
                    <span style={{ background: '#34d399', color: '#0f1117', borderRadius: 10, padding: '1px 7px', fontSize: 10, fontWeight: 700 }}>
                      {chat.unread}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: '#475569', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {chat.jid}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Main: messages + compose */}
        <div style={s.main}>
          {!selected ? (
            <div style={s.placeholder}>
              Selecione uma conversa para ver as mensagens
            </div>
          ) : (
            <>
              {/* Chat header */}
              <div style={s.chatHead}>
                <div style={{ fontWeight: 700, fontSize: 15 }}>{selected.name || selected.jid.split('@')[0]}</div>
                <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>{selected.jid}</div>
              </div>

              {/* Messages */}
              <div
                ref={msgListRef}
                style={{ ...s.msgList, overflowY: 'auto' } as React.CSSProperties}
                onScroll={e => {
                  const el = e.currentTarget
                  if (el.scrollTop < 50 && hasMore && !loadingMore) handleScrollTop()
                }}
              >
                {loadingMore && (
                  <div style={{ textAlign: 'center', color: '#475569', fontSize: 12, padding: '8px 0' }}>
                    Carregando mensagens mais antigas…
                  </div>
                )}
                {msgsLoading && messages.length === 0 && (
                  <div style={{ color: '#475569', fontSize: 13, textAlign: 'center', margin: 'auto' }}>
                    Carregando mensagens…
                  </div>
                )}
                {!msgsLoading && messages.length === 0 && (
                  <div style={{ color: '#475569', fontSize: 13, textAlign: 'center', margin: 'auto' }}>
                    Sem mensagens recentes
                  </div>
                )}
                {messages.map(m => (
                  <div key={m.id} style={{ display: 'flex', flexDirection: 'column', alignItems: m.fromMe ? 'flex-end' : 'flex-start' }}>
                    <div style={s.bubble(m.fromMe)}>
                      {m.mediaType === 'image' ? (
                        <div>
                          {mediaUrls[m.id] ? (
                            <img
                              src={mediaUrls[m.id]}
                              alt={m.caption || 'image'}
                              style={{ maxWidth: '100%', borderRadius: 6, display: 'block', cursor: 'pointer' }}
                              onClick={() => window.open(mediaUrls[m.id], '_blank')}
                            />
                          ) : (
                            <div
                              style={{
                                width: 200, height: 150, background: '#1a1a2e', borderRadius: 6,
                                display: 'flex', flexDirection: 'column', alignItems: 'center',
                                justifyContent: 'center', cursor: mediaLoading[m.id] ? 'wait' : 'pointer',
                                color: mediaLoading[m.id] ? '#7c3aed' : '#475569', fontSize: 12, gap: 4,
                              }}
                              onClick={mediaLoading[m.id] ? undefined : () => loadMedia(selected?.jid || '', m.id)}
                            >
                              {mediaLoading[m.id]
                                ? '⏳ baixando…'
                                : msgsLoading
                                ? 'aguarde…'
                                : '📷 clique pra abrir'}
                            </div>
                          )}
                          {m.caption && <div style={{ marginTop: 4, fontSize: 13 }}>{m.caption}</div>}
                        </div>
                      ) : m.mediaType === 'video' ? (
                        <div>
                          {mediaUrls[m.id] ? (
                            <video
                              controls
                              src={mediaUrls[m.id]}
                              style={{ maxWidth: '100%', borderRadius: 6, display: 'block' }}
                            />
                          ) : (
                            <div
                              style={{
                                width: 200, height: 80, background: '#1a1a2e', borderRadius: 6,
                                display: 'flex', flexDirection: 'column', alignItems: 'center',
                                justifyContent: 'center', cursor: mediaLoading[m.id] ? 'wait' : 'pointer',
                                color: mediaLoading[m.id] ? '#7c3aed' : '#64748b', fontSize: 12, gap: 4,
                              }}
                              onClick={mediaLoading[m.id] ? undefined : () => loadMedia(selected?.jid || '', m.id)}
                            >
                              {mediaLoading[m.id] ? '⏳ baixando…' : '🎥 clique pra abrir'}
                            </div>
                          )}
                          {m.caption && <div style={{ marginTop: 4, fontSize: 13 }}>{m.caption}</div>}
                        </div>
                      ) : m.mediaType === 'audio' || m.mediaType === 'ptt' ? (
                        <div>
                          {mediaUrls[m.id] ? (
                            <AudioPlayer src={mediaUrls[m.id]} fileName={m.fileName} />
                          ) : (
                            <div
                              style={{
                                padding: '10px 16px', background: '#1a1a2e', borderRadius: 6,
                                display: 'flex', alignItems: 'center', gap: 8, cursor: mediaLoading[m.id] ? 'wait' : 'pointer',
                                color: mediaLoading[m.id] ? '#7c3aed' : '#64748b', fontSize: 12,
                              }}
                              onClick={mediaLoading[m.id] ? undefined : () => loadMedia(selected?.jid || '', m.id)}
                            >
                              {mediaLoading[m.id] ? '⏳ baixando…' : '🎵 clique pra ouvir'}
                            </div>
                          )}
                        </div>
                      ) : m.mediaType === 'document' ? (
                        <div>
                          {mediaUrls[m.id] ? (
                            <a href={mediaUrls[m.id]} download={m.fileName || 'document'} target="_blank" rel="noreferrer"
                              style={{ color: '#7c3aed', fontSize: 13, textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 6 }}>
                              📎 {m.fileName || 'baixar documento'}
                            </a>
                          ) : (
                            <div
                              style={{
                                padding: '10px 16px', background: '#1a1a2e', borderRadius: 6,
                                cursor: mediaLoading[m.id] ? 'wait' : 'pointer',
                                color: mediaLoading[m.id] ? '#7c3aed' : '#64748b', fontSize: 12,
                              }}
                              onClick={mediaLoading[m.id] ? undefined : () => loadMedia(selected?.jid || '', m.id)}
                            >
                              {mediaLoading[m.id] ? '⏳ baixando…' : `📎 ${m.fileName || 'baixar documento'}`}
                            </div>
                          )}
                        </div>
                      ) : m.mediaType === 'sticker' ? (
                        <div>
                          {mediaUrls[m.id] ? (
                            <img src={mediaUrls[m.id]} alt="sticker" style={{ maxWidth: 200, borderRadius: 6 }} />
                          ) : (
                            <div
                              style={{
                                width: 150, height: 150, background: '#1a1a2e', borderRadius: 6,
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                cursor: mediaLoading[m.id] ? 'wait' : 'pointer', color: mediaLoading[m.id] ? '#7c3aed' : '#64748b', fontSize: 12,
                              }}
                              onClick={mediaLoading[m.id] ? undefined : () => loadMedia(selected?.jid || '', m.id)}
                            >
                              {mediaLoading[m.id] ? '⏳' : '😀 sticker'}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span>{m.body}</span>
                      )}
                      <div style={s.bubbleTime}>{m.timestamp ? fmtTime(m.timestamp) : ''}</div>
                    </div>
                  </div>
                ))}
              </div>

              {/* Compose */}
              <div style={s.compose}>
                <textarea
                  value={composerText}
                  onChange={e => setComposerText(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="Digite sua mensagem… (Ctrl+Enter para enviar)"
                  rows={3}
                  style={s.textarea}
                />
                <div style={{ display: 'flex', alignItems: 'center', marginTop: 8, gap: 12 }}>
                  <button
                    onClick={handleSend}
                    disabled={!canSend}
                    style={canSend ? s.btn : s.btnDisabled}
                  >
                    {sending ? 'Enviando…' : 'Enviar'}
                  </button>
                  <span style={{ fontSize: 11, color: '#334155' }}>Ctrl+Enter</span>
                  {statusMsg && (
                    <span style={{ fontSize: 13, color: statusMsg.type === 'success' ? '#34d399' : '#f87171', marginLeft: 8 }}>
                      {statusMsg.text}
                    </span>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
