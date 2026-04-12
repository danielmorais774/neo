import { useEffect, useState, useCallback } from 'react'

const API = 'http://localhost:8000/api/v1'

// ── Types ────────────────────────────────────────────────────────────

interface SessionLogFile {
  filename: string
  session_id: string
  created_at: string | null  // "2026-03-26 21:14:00" or null for old files
  size_bytes: number
  mtime: number              // Unix timestamp (seconds)
}

// ── Helpers ──────────────────────────────────────────────────────────

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function fmtDate(mtime: number): string {
  return new Date(mtime * 1000).toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function shortId(sessionId: string): string {
  // Return a readable short version: first 8 + last 4 chars
  if (sessionId.length <= 14) return sessionId
  return `${sessionId.slice(0, 8)}…${sessionId.slice(-4)}`
}

// ── Simple Markdown renderer ─────────────────────────────────────────

const COLORS = {
  h1: '#a78bfa',
  h2: '#818cf8',
  h3: '#6ee7b7',
  h4: '#fbbf24',
  codeFence: '#f59e0b',
  codeInline: '#34d399',
  bold: '#e2e8f0',
  bullet: '#94a3b8',
  separator: '#1e2235',
  detail: '#475569',
  default: '#cbd5e1',
  muted: '#64748b',
  duration: '#fb923c',
  success: '#34d399',
  error: '#f87171',
}

/** Render inline markdown tokens: **bold**, `code`, durations like 1234.5ms */
function renderInline(text: string, _baseColor: string = COLORS.default): React.ReactNode[] {
  const parts: React.ReactNode[] = []
  // Match **bold**, `code`, or NN.Nms / NNms durations
  const rx = /(\*\*(.+?)\*\*)|(`([^`]+?)`)|(\b\d+\.?\d*ms\b)/g
  let last = 0
  let match: RegExpExecArray | null
  let idx = 0
  while ((match = rx.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(<span key={idx++}>{text.slice(last, match.index)}</span>)
    }
    if (match[1]) {
      // **bold**
      parts.push(<span key={idx++} style={{ color: COLORS.bold, fontWeight: 600 }}>{match[2]}</span>)
    } else if (match[3]) {
      // `code`
      parts.push(<span key={idx++} style={{ color: COLORS.codeInline, background: '#0a0c14', padding: '1px 4px', borderRadius: 3, fontFamily: 'monospace', fontSize: 11 }}>{match[4]}</span>)
    } else if (match[5]) {
      // duration like 1234.5ms
      const ms = parseFloat(match[5])
      const color = ms > 5000 ? COLORS.error : ms > 1000 ? COLORS.duration : COLORS.success
      parts.push(<span key={idx++} style={{ color, fontWeight: 600, fontFamily: 'monospace' }}>{match[5]}</span>)
    }
    last = match.index + match[0].length
  }
  if (last < text.length) {
    parts.push(<span key={idx++}>{text.slice(last)}</span>)
  }
  return parts
}

function renderLines(text: string): React.ReactNode[] {
  const lines = text.split('\n')
  const nodes: React.ReactNode[] = []
  let inCode = false

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // ── Code fences ──
    if (line.startsWith('```')) {
      inCode = !inCode
      const lang = inCode ? line.slice(3).trim() : ''
      nodes.push(
        <div key={i} style={{ color: COLORS.codeFence, fontFamily: 'monospace', fontSize: 10, lineHeight: 1.6, userSelect: 'text', opacity: 0.5 }}>
          {inCode ? (lang ? `── ${lang} ──` : '─────') : '─────'}
        </div>
      )
      continue
    }

    if (inCode) {
      nodes.push(
        <div key={i} style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 11, lineHeight: 1.6, background: '#0a0c14', paddingLeft: 12, userSelect: 'text', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
          {line || '\u00a0'}
        </div>
      )
      continue
    }

    // ── <details>/<summary> → collapsible ──
    if (line.startsWith('<details>')) {
      continue
    }
    if (line.startsWith('</details>')) {
      continue
    }
    if (line.startsWith('<summary>')) {
      const summaryText = line.replace(/<\/?summary>/g, '').trim()
      const toggleId = `details-${i}`
      nodes.push(
        <div
          key={i}
          onClick={() => {
            const el = document.getElementById(toggleId)
            if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none'
          }}
          style={{
            color: '#818cf8', fontSize: 11, cursor: 'pointer', padding: '4px 0',
            fontWeight: 500, userSelect: 'none', fontFamily: 'sans-serif',
          }}
        >
          {'▸ '}{summaryText}
        </div>
      )
      // Collect content until </details>
      const detailLines: string[] = []
      let j = i + 1
      while (j < lines.length && !lines[j].startsWith('</details>')) {
        detailLines.push(lines[j])
        j++
      }
      nodes.push(
        <div key={`${i}-body`} id={toggleId} style={{ display: 'none', marginLeft: 12, borderLeft: '2px solid #1e2235', paddingLeft: 10 }}>
          {renderLines(detailLines.join('\n'))}
        </div>
      )
      i = j // skip to </details>
      continue
    }

    if (line === '---') {
      nodes.push(<hr key={i} style={{ border: 'none', borderTop: `1px solid ${COLORS.separator}`, margin: '10px 0' }} />)
      continue
    }

    // ── Headings ──
    if (line.startsWith('# ')) {
      nodes.push(
        <div key={i} style={{ color: COLORS.h1, fontSize: 16, fontWeight: 700, marginTop: 20, marginBottom: 6, fontFamily: 'sans-serif' }}>
          {renderInline(line.slice(2), COLORS.h1)}
        </div>
      )
      continue
    }

    if (line.startsWith('## ')) {
      nodes.push(
        <div key={i} style={{ color: COLORS.h2, fontSize: 14, fontWeight: 600, marginTop: 14, marginBottom: 4, borderBottom: `1px solid #1e2235`, paddingBottom: 4, fontFamily: 'sans-serif' }}>
          {renderInline(line.slice(3), COLORS.h2)}
        </div>
      )
      continue
    }

    if (line.startsWith('### ')) {
      nodes.push(
        <div key={i} style={{ color: COLORS.h3, fontSize: 13, fontWeight: 600, marginTop: 10, marginBottom: 2, fontFamily: 'sans-serif' }}>
          {renderInline(line.slice(4), COLORS.h3)}
        </div>
      )
      continue
    }

    if (line.startsWith('#### ')) {
      nodes.push(
        <div key={i} style={{ color: COLORS.h4, fontSize: 12, fontWeight: 600, marginTop: 8, fontFamily: 'sans-serif' }}>
          {renderInline(line.slice(5), COLORS.h4)}
        </div>
      )
      continue
    }

    // ── Bullets with bold ──
    if (line.startsWith('- **') || line.startsWith('* **')) {
      nodes.push(
        <div key={i} style={{ fontSize: 12, paddingLeft: 10, lineHeight: 1.7 }}>
          {renderInline(line, COLORS.bold)}
        </div>
      )
      continue
    }

    if (line.startsWith('- ') || line.startsWith('* ')) {
      nodes.push(
        <div key={i} style={{ fontSize: 12, paddingLeft: 10, lineHeight: 1.7, color: COLORS.bullet }}>
          {renderInline(line, COLORS.bullet)}
        </div>
      )
      continue
    }

    if (line.trim() === '') {
      nodes.push(<div key={i} style={{ height: 6 }} />)
      continue
    }

    // ── Default text with inline rendering ──
    nodes.push(
      <div key={i} style={{ color: COLORS.default, fontSize: 12, lineHeight: 1.7, wordBreak: 'break-word', fontFamily: 'monospace' }}>
        {renderInline(line)}
      </div>
    )
  }

  return nodes
}

// ── Main Component ────────────────────────────────────────────────────

export default function SessionLogsPanel() {
  const [files, setFiles] = useState<SessionLogFile[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState<string>('')
  const [contentLoading, setContentLoading] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [filter, setFilter] = useState('')

  // ── Fetch file list ──────────────────────

  const fetchList = useCallback(async () => {
    try {
      const res = await fetch(`${API}/session-logs`)
      if (!res.ok) return
      const data = await res.json()
      setFiles(data.files ?? [])
    } catch { /* network unavailable */ }
  }, [])

  // ── Fetch file content ───────────────────

  const fetchContent = useCallback(async (filename: string) => {
    setContentLoading(true)
    try {
      const res = await fetch(`${API}/session-logs/${encodeURIComponent(filename)}`)
      setContent(res.ok ? await res.text() : `Erro ao carregar: ${res.status}`)
    } catch {
      setContent('Falha na conexão com o servidor.')
    } finally {
      setContentLoading(false)
    }
  }, [])

  // ── Auto-refresh: list ───────────────────

  useEffect(() => {
    fetchList()
  }, [fetchList])

  useEffect(() => {
    if (!autoRefresh) return
    const iv = setInterval(fetchList, 5000)
    return () => clearInterval(iv)
  }, [autoRefresh, fetchList])

  // ── Auto-refresh: content ────────────────

  useEffect(() => {
    if (!selected || !autoRefresh) return
    const iv = setInterval(() => fetchContent(selected), 5000)
    return () => clearInterval(iv)
  }, [selected, autoRefresh, fetchContent])

  // ── Select file ──────────────────────────

  const selectFile = (filename: string) => {
    setSelected(filename)
    setContent('')
    fetchContent(filename)
  }

  // ── Filter ───────────────────────────────

  const filtered = filter
    ? files.filter(f => f.filename.toLowerCase().includes(filter.toLowerCase()) || f.session_id.toLowerCase().includes(filter.toLowerCase()))
    : files

  // ── Render ───────────────────────────────

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 41px)', background: '#0f1117', fontFamily: 'monospace' }}>

      {/* ── Left sidebar: file list ── */}
      <div style={{ width: 320, borderRight: '1px solid #1e2235', display: 'flex', flexDirection: 'column', flexShrink: 0, background: '#0c0f1a' }}>

        {/* Header */}
        <div style={{ padding: '12px 14px', borderBottom: '1px solid #1e2235', background: '#13161f' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ color: '#a78bfa', fontWeight: 700, fontSize: 12, fontFamily: 'sans-serif' }}>
              📝 Session Logs <span style={{ color: '#475569' }}>({filtered.length})</span>
            </span>
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                onClick={fetchList}
                title="Atualizar lista"
                style={{ background: '#1e2235', border: 'none', color: '#94a3b8', padding: '3px 8px', borderRadius: 4, cursor: 'pointer', fontSize: 11 }}
              >
                ↻
              </button>
              <button
                onClick={() => setAutoRefresh(v => !v)}
                title={autoRefresh ? 'Desativar auto-refresh' : 'Ativar auto-refresh'}
                style={{
                  background: autoRefresh ? '#3b1f6e' : '#1e2235',
                  border: 'none',
                  color: autoRefresh ? '#a78bfa' : '#475569',
                  padding: '3px 8px', borderRadius: 4, cursor: 'pointer', fontSize: 10,
                }}
              >
                {autoRefresh ? '⏸ live' : '▶ live'}
              </button>
            </div>
          </div>

          <input
            placeholder="Filtrar session ID..."
            value={filter}
            onChange={e => setFilter(e.target.value)}
            style={{
              width: '100%', background: '#0f1117', border: '1px solid #1e2235',
              color: '#94a3b8', padding: '5px 8px', borderRadius: 4, fontSize: 11,
              boxSizing: 'border-box', outline: 'none',
            }}
          />
        </div>

        {/* File list */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {filtered.length === 0 && (
            <div style={{ color: '#2a2d3e', padding: 24, textAlign: 'center', fontSize: 12 }}>
              Nenhum log encontrado
            </div>
          )}

          {filtered.map(f => {
            const isActive = selected === f.filename
            return (
              <div
                key={f.filename}
                onClick={() => selectFile(f.filename)}
                style={{
                  padding: '9px 14px',
                  cursor: 'pointer',
                  borderBottom: '1px solid #0f1117',
                  background: isActive ? '#16192a' : 'transparent',
                  borderLeft: isActive ? '2px solid #7c3aed' : '2px solid transparent',
                  transition: 'background 0.1s',
                }}
              >
                {/* Timestamp (primary label) */}
                <div style={{ color: isActive ? '#a78bfa' : '#818cf8', fontSize: 11, fontWeight: 600, marginBottom: 2, fontFamily: 'sans-serif' }}>
                  {f.created_at ?? fmtDate(f.mtime)}
                </div>

                {/* Short session ID */}
                <div style={{ color: '#475569', fontSize: 10, fontFamily: 'monospace', marginBottom: 3 }}>
                  {shortId(f.session_id)}
                </div>

                {/* Size + mtime */}
                <div style={{ color: '#2a3551', fontSize: 10 }}>
                  {fmtSize(f.size_bytes)} · {fmtDate(f.mtime)}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* ── Right pane: content viewer ── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 28px', position: 'relative' }}>

        {/* Empty state */}
        {!selected && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#1e2235', gap: 12 }}>
            <span style={{ fontSize: 40 }}>📋</span>
            <span style={{ fontSize: 14, fontFamily: 'sans-serif' }}>Selecione um arquivo para visualizar</span>
          </div>
        )}

        {/* Loading indicator on top-right */}
        {contentLoading && (
          <div style={{ position: 'absolute', top: 12, right: 16, color: '#475569', fontSize: 11 }}>
            carregando…
          </div>
        )}

        {/* Markdown content */}
        {selected && content && (
          <div>
            {/* File header bar */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, paddingBottom: 10, borderBottom: '1px solid #1e2235' }}>
              <span style={{ color: '#475569', fontSize: 11, fontFamily: 'monospace', wordBreak: 'break-all' }}>
                {selected}
              </span>
              <span style={{ color: '#2a3551', fontSize: 10, flexShrink: 0, marginLeft: 12 }}>
                {fmtSize(content.length)} chars
              </span>
            </div>

            {/* Rendered lines */}
            {renderLines(content)}
          </div>
        )}
      </div>
    </div>
  )
}
