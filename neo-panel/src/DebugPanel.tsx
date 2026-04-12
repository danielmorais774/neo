import { useEffect, useState, useCallback } from 'react'
import './debug.css'

const API = 'http://localhost:8000/api/v1/debug'

// ── Types ────────────────────────────────────────────────────────────

interface DebugSession {
  session_id: string
  service: string
  status: string
  message_count: number
  created_at: string
  last_activity_at: string
}

interface ClassifierPayload {
  messages?: { role: string; content: string }[]
  raw_response?: string
  error?: string
}

interface IntentRouting {
  intent: string
  confidence: number
  explanation: string
  tool_name?: string
  duration_ms: number
  classifier_payload?: ClassifierPayload
  needs_memory?: boolean
}

interface TopicRotation {
  should_rotate?: boolean
  similarity?: number | null
  threshold?: number
  reason?: string
  message_count?: number
  min_required?: number
  cooldown_minutes?: number
  elapsed_minutes?: number | null
  context_text?: string
  duration_ms?: number
}

interface ContextRecoveryEntry {
  content: string
  score: number
  source: string
  session_id?: string
  created_at?: string
}

interface LLMCall {
  provider: string
  model: string
  stage?: string
  temperature?: number | null
  messages_sent: { role: string; content: string }[]
  response_text: string
  duration_ms: number
  tokens: { prompt: number; completion: number; total: number }
  error?: string
  timestamp: string
}

interface ToolCall {
  tool_name: string
  arguments: object
  result: unknown
  duration_ms: number
  error?: string
  timestamp?: string
}

interface MessageTrace {
  trace_id?: string
  session_id: string
  user_message: string
  started_at: string
  duration_ms: number
  intent_routing: IntentRouting
  topic_rotation?: TopicRotation
  context_recovery?: ContextRecoveryEntry[]
  llm_calls: LLMCall[]
  tool_calls: ToolCall[]
  final_response?: string
  error?: string
}

interface Stats {
  mongo_available: boolean
  total_sessions: number
  total_traces: number
  intent_distribution: Record<string, number>
  avg_llm_duration_ms: number
}

// ── Timeline step union ───────────────────────────────────────────────

type TimelineStep =
  | { kind: 'topic_rotation'; data: TopicRotation }
  | { kind: 'intent'; data: IntentRouting }
  | { kind: 'context_recovery'; data: ContextRecoveryEntry[]; needsMemory: boolean }
  | { kind: 'tool'; seqIdx: number; data: ToolCall }
  | { kind: 'llm'; seqIdx: number; data: LLMCall }
  | { kind: 'final'; text: string }
  | { kind: 'error'; text: string }

function buildTimeline(trace: MessageTrace): TimelineStep[] {
  const steps: TimelineStep[] = []

  // 0. Topic rotation check — before intent
  if (trace.topic_rotation) {
    steps.push({ kind: 'topic_rotation', data: trace.topic_rotation })
  }

  // 1. Intent routing
  if (trace.intent_routing) {
    steps.push({ kind: 'intent', data: trace.intent_routing })
  }

  // 1b. Context recovery — after intent (since needs_memory comes from intent)
  const needsMemory = trace.intent_routing?.needs_memory !== false
  if (trace.context_recovery || !needsMemory) {
    steps.push({
      kind: 'context_recovery',
      data: trace.context_recovery || [],
      needsMemory,
    })
  }

  // Classifier LLM call has provider='router' or model='intent-classifier'
  const isClassifier = (c: LLMCall) =>
    c.provider === 'router' || c.model === 'intent-classifier'

  // Merge non-classifier LLM calls + tool calls, sort by timestamp
  type Timed = { ts: number; step: TimelineStep }
  const pool: Timed[] = []

  trace.llm_calls?.forEach((call, i) => {
    if (!isClassifier(call)) {
      pool.push({
        ts: call.timestamp ? new Date(call.timestamp).getTime() : i,
        step: { kind: 'llm', seqIdx: i, data: call },
      })
    }
  })

  trace.tool_calls?.forEach((tc, i) => {
    pool.push({
      ts: tc.timestamp ? new Date(tc.timestamp).getTime() : i,
      step: { kind: 'tool', seqIdx: i, data: tc },
    })
  })

  pool.sort((a, b) => a.ts - b.ts)
  pool.forEach(p => steps.push(p.step))

  if (trace.final_response) steps.push({ kind: 'final', text: trace.final_response })
  if (trace.error) steps.push({ kind: 'error', text: trace.error })

  return steps
}

// ── Helpers ──────────────────────────────────────────────────────────

function fmtTime(iso: string) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function fmtMs(ms: number) {
  if (!ms && ms !== 0) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.round(ms)}ms`
}

function codeStr(val: unknown): string {
  if (val === null || val === undefined) return ''
  if (typeof val === 'string') return val
  return JSON.stringify(val, null, 2)
}

// ── Expandable code block ─────────────────────────────────────────────

function CodeBlock({ content, maxLines = 20 }: { content: string; maxLines?: number }) {
  const [expanded, setExpanded] = useState(false)
  const lines = content.split('\n')
  const visible = expanded ? content : lines.slice(0, maxLines).join('\n')
  const needsToggle = lines.length > maxLines

  return (
    <div className="code-block">
      <pre>{visible}</pre>
      {needsToggle && (
        <button className="code-expand-btn" onClick={() => setExpanded(e => !e)}>
          {expanded ? '▲ Recolher' : `▼ Mostrar mais (${lines.length - maxLines} linhas)`}
        </button>
      )}
    </div>
  )
}

// ── Messages viewer (role + content) ─────────────────────────────────

function MessagesViewer({ messages }: { messages: { role: string; content: string }[] }) {
  const [openIdx, setOpenIdx] = useState<Set<number>>(new Set(messages.map((_, i) => i)))
  const toggle = (i: number) => setOpenIdx(prev => {
    const next = new Set(prev)
    next.has(i) ? next.delete(i) : next.add(i)
    return next
  })

  return (
    <div className="messages-viewer">
      {messages.map((m, i) => (
        <div key={i} className={`msg-block msg-${m.role}`}>
          <div className="msg-block-header" onClick={() => toggle(i)}>
            <span className={`msg-role-tag role-${m.role}`}>{m.role.toUpperCase()}</span>
            <span className="msg-char-count">{m.content.length} chars</span>
            <span className="expand-chevron">{openIdx.has(i) ? '▲' : '▼'}</span>
          </div>
          {openIdx.has(i) && (
            <div className="msg-block-body">
              <CodeBlock content={m.content} maxLines={30} />
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ── Step badge ────────────────────────────────────────────────────────

function StepBadge({ n, color }: { n: number; color: string }) {
  return (
    <div className="step-badge" style={{ background: color }}>
      {n}
    </div>
  )
}

// ── Intent step ───────────────────────────────────────────────────────

function IntentStep({ step, n, open, onToggle }: {
  step: Extract<TimelineStep, { kind: 'intent' }>
  n: number
  open: boolean
  onToggle: () => void
}) {
  const d = step.data
  const intent = d.intent || 'UNKNOWN'
  const conf = ((d.confidence || 0) * 100).toFixed(0)
  const payload = d.classifier_payload
  const needsMemory = d.needs_memory

  return (
    <div className={`step-block step-intent ${open ? 'open' : ''}`}>
      <div className="step-header" onClick={onToggle}>
        <StepBadge n={n} color="#7c3aed" />
        <span className="step-icon">🧭</span>
        <span className="step-title">Intent Classification</span>
        <span className={`intent-badge intent-${intent}`}>{intent.replace(/_/g, ' ')}</span>
        <span className="step-conf">{conf}%</span>
        {needsMemory === false && <span className="step-tag-muted">🚫 no memory</span>}
        {needsMemory === true && <span className="step-tag-ok">🧠 memory</span>}
        {d.tool_name && <span className="step-tool-tag">{d.tool_name}</span>}
        <span className="step-dur">{fmtMs(d.duration_ms)}</span>
        <span className="expand-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="step-body">
          {d.explanation && (
            <div className="step-row">
              <span className="step-key">Explicação</span>
              <span className="step-val">{d.explanation}</span>
            </div>
          )}
          <div className="step-row">
            <span className="step-key">Needs Memory</span>
            <span className="step-val" style={{ color: needsMemory === false ? '#f87171' : '#4ade80' }}>
              {needsMemory === false ? 'false — memórias antigas não serão recuperadas' : 'true — recuperação de contexto ativa'}
            </span>
          </div>
          {payload && (
            <>
              {/* RAW JSON payload — full request representation */}
              <div className="step-sub-label">RAW JSON Request (enviado ao classificador)</div>
              <CodeBlock content={JSON.stringify({
                model: 'intent-classifier',
                temperature: 0.1,
                max_tokens: 300,
                messages: payload.messages?.map(m => ({
                  role: m.role,
                  content: m.content.length > 200 ? m.content.slice(0, 200) + '…' : m.content,
                })),
              }, null, 2)} maxLines={15} />

              <div className="step-sub-label" style={{ marginTop: 10 }}>Mensagens enviadas ({payload.messages?.length || 0})</div>
              {payload.messages && payload.messages.length > 0 ? (
                <MessagesViewer messages={payload.messages} />
              ) : (
                <div className="step-empty">Sem mensagens registradas</div>
              )}
              {payload.raw_response && (
                <>
                  <div className="step-sub-label" style={{ marginTop: 10 }}>Resposta RAW do classificador</div>
                  <CodeBlock content={payload.raw_response} maxLines={20} />
                </>
              )}
              {payload.error && (
                <div className="step-error-inline">❌ Erro do classificador: {payload.error}</div>
              )}
            </>
          )}
          {!payload && (
            <div className="step-empty">classifier_payload não disponível (versão antiga do trace)</div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Tool step ─────────────────────────────────────────────────────────

function ToolStep({ step, n, open, onToggle }: {
  step: Extract<TimelineStep, { kind: 'tool' }>
  n: number
  open: boolean
  onToggle: () => void
}) {
  const tc = step.data
  const resultStr = codeStr(tc.result)
  const argsStr = JSON.stringify(tc.arguments, null, 2)

  return (
    <div className={`step-block step-tool ${open ? 'open' : ''}`}>
      <div className="step-header" onClick={onToggle}>
        <StepBadge n={n} color="#b45309" />
        <span className="step-icon">🔧</span>
        <span className="step-title">Tool Call</span>
        <span className="step-tool-tag">{tc.tool_name}</span>
        {tc.error
          ? <span className="step-status-err">❌ erro</span>
          : <span className="step-status-ok">✅</span>}
        {tc.duration_ms > 0 && <span className="step-dur">{fmtMs(tc.duration_ms)}</span>}
        {tc.timestamp && <span className="step-time">{fmtTime(tc.timestamp)}</span>}
        <span className="expand-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="step-body">
          <div className="step-sub-label">Argumentos</div>
          <CodeBlock content={argsStr} maxLines={25} />
          {resultStr && (
            <>
              <div className="step-sub-label" style={{ marginTop: 10 }}>
                Resultado {tc.error ? <span style={{ color: '#f87171' }}>(erro)</span> : ''}
              </div>
              <CodeBlock content={tc.error || resultStr} maxLines={30} />
            </>
          )}
          {tc.error && !resultStr && (
            <>
              <div className="step-sub-label" style={{ marginTop: 10, color: '#f87171' }}>Erro</div>
              <div className="step-error-inline">{tc.error}</div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── LLM step ──────────────────────────────────────────────────────────

function LLMStep({ step, n, open, onToggle }: {
  step: Extract<TimelineStep, { kind: 'llm' }>
  n: number
  open: boolean
  onToggle: () => void
}) {
  const call = step.data

  return (
    <div className={`step-block step-llm ${open ? 'open' : ''}`}>
      <div className="step-header" onClick={onToggle}>
        <StepBadge n={n} color="#0e7490" />
        <span className="step-icon">🤖</span>
        <span className="step-title">LLM Call</span>
        {call.stage && <span className="step-stage-tag">{call.stage}</span>}
        <span className="step-model-tag">{call.model}</span>
        <span className="step-provider">{call.provider}</span>
        {call.temperature != null && (
          <span className="step-temp-tag">temp={call.temperature}</span>
        )}
        {call.tokens?.total > 0 && (
          <span className="step-tokens">{call.tokens.total} tokens</span>
        )}
        {call.error
          ? <span className="step-status-err">❌</span>
          : <span className="step-status-ok">✅</span>}
        <span className="step-dur">{fmtMs(call.duration_ms)}</span>
        {call.timestamp && <span className="step-time">{fmtTime(call.timestamp)}</span>}
        <span className="expand-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="step-body">
          <div className="step-row" style={{ marginBottom: 8 }}>
            <span className="step-key">Provider</span>
            <span className="step-val">{call.provider}</span>
          </div>
          <div className="step-row" style={{ marginBottom: 8 }}>
            <span className="step-key">Model</span>
            <span className="step-val">{call.model}</span>
          </div>
          {call.temperature != null && (
            <div className="step-row" style={{ marginBottom: 8 }}>
              <span className="step-key">Temperature</span>
              <span className="step-val">{call.temperature}</span>
            </div>
          )}
          {call.stage && (
            <div className="step-row" style={{ marginBottom: 8 }}>
              <span className="step-key">Stage</span>
              <span className="step-val">{call.stage}</span>
            </div>
          )}
          {call.tokens?.total > 0 && (
            <div className="step-row" style={{ marginBottom: 8 }}>
              <span className="step-key">Tokens</span>
              <span className="step-val">
                prompt: {call.tokens.prompt} + completion: {call.tokens.completion} = {call.tokens.total}
              </span>
            </div>
          )}

          {/* Raw JSON payload — full request representation */}
          <div className="step-sub-label">RAW JSON Payload</div>
          <CodeBlock content={JSON.stringify({
            model: call.model,
            temperature: call.temperature,
            messages: call.messages_sent?.map(m => ({
              role: m.role,
              content: m.content.length > 200 ? m.content.slice(0, 200) + '…' : m.content,
            })),
            stream: true,
          }, null, 2)} maxLines={15} />

          <div className="step-sub-label" style={{ marginTop: 10 }}>Mensagens enviadas ({call.messages_sent?.length || 0})</div>
          {call.messages_sent?.length > 0 ? (
            <MessagesViewer messages={call.messages_sent} />
          ) : (
            <div className="step-empty">Sem mensagens registradas</div>
          )}
          {call.response_text && (
            <>
              <div className="step-sub-label" style={{ marginTop: 10 }}>Resposta recebida</div>
              <CodeBlock content={call.response_text} maxLines={25} />
            </>
          )}
          {call.error && (
            <div className="step-error-inline" style={{ marginTop: 8 }}>❌ {call.error}</div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Topic rotation step ───────────────────────────────────────────────

function TopicRotationStep({ step, n, open, onToggle }: {
  step: Extract<TimelineStep, { kind: 'topic_rotation' }>
  n: number
  open: boolean
  onToggle: () => void
}) {
  const d = step.data
  const didRotate = d.should_rotate === true
  const similarity = d.similarity != null ? (d.similarity * 100).toFixed(1) + '%' : '—'
  const threshold = d.threshold != null ? (d.threshold * 100).toFixed(1) + '%' : '—'

  return (
    <div className={`step-block step-rotation ${open ? 'open' : ''}`}>
      <div className="step-header" onClick={onToggle}>
        <StepBadge n={n} color="#854d0e" />
        <span className="step-icon">🔄</span>
        <span className="step-title">Topic Rotation Check</span>
        {didRotate
          ? <span className="step-tag-err">ROTACIONOU</span>
          : <span className="step-tag-ok">mesma sessão</span>}
        {d.similarity != null && <span className="step-conf">sim={similarity}</span>}
        {d.duration_ms != null && d.duration_ms > 0 && <span className="step-dur">{fmtMs(d.duration_ms)}</span>}
        <span className="expand-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="step-body">
          <div className="step-row">
            <span className="step-key">Rotacionou</span>
            <span className="step-val" style={{ color: didRotate ? '#f87171' : '#4ade80' }}>
              {didRotate ? 'Sim — nova sessão criada' : 'Não — mesma sessão mantida'}
            </span>
          </div>
          {d.reason && (
            <div className="step-row">
              <span className="step-key">Razão</span>
              <span className="step-val">{d.reason}</span>
            </div>
          )}
          <div className="step-row">
            <span className="step-key">Similaridade</span>
            <span className="step-val">{similarity}</span>
          </div>
          <div className="step-row">
            <span className="step-key">Threshold</span>
            <span className="step-val">{threshold}</span>
          </div>
          <div className="step-row">
            <span className="step-key">Mensagens</span>
            <span className="step-val">{d.message_count ?? '—'} (mínimo: {d.min_required ?? '—'})</span>
          </div>
          <div className="step-row">
            <span className="step-key">Cooldown</span>
            <span className="step-val">{d.cooldown_minutes ?? '—'}min (decorrido: {d.elapsed_minutes != null ? d.elapsed_minutes.toFixed(1) + 'min' : '—'})</span>
          </div>
          {d.context_text && (
            <>
              <div className="step-sub-label" style={{ marginTop: 8 }}>Texto de contexto usado no cálculo</div>
              <CodeBlock content={d.context_text} maxLines={10} />
            </>
          )}
          <div className="step-sub-label" style={{ marginTop: 8 }}>Dados completos (RAW)</div>
          <CodeBlock content={JSON.stringify(d, null, 2)} maxLines={20} />
        </div>
      )}
    </div>
  )
}

// ── Context recovery step ─────────────────────────────────────────────

function ContextRecoveryStep({ step, n, open, onToggle }: {
  step: Extract<TimelineStep, { kind: 'context_recovery' }>
  n: number
  open: boolean
  onToggle: () => void
}) {
  const entries = step.data
  const skipped = !step.needsMemory

  return (
    <div className={`step-block step-recovery ${open ? 'open' : ''}`}>
      <div className="step-header" onClick={onToggle}>
        <StepBadge n={n} color="#065f46" />
        <span className="step-icon">🧠</span>
        <span className="step-title">Context Recovery</span>
        {skipped
          ? <span className="step-tag-muted">PULADO (needs_memory=false)</span>
          : <span className="step-tag-ok">{entries.length} memória{entries.length !== 1 ? 's' : ''}</span>}
        <span className="expand-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="step-body">
          {skipped ? (
            <div className="step-empty">
              Recuperação de memória pulada — o classificador indicou que esta mensagem não precisa de contexto de sessões anteriores.
            </div>
          ) : entries.length === 0 ? (
            <div className="step-empty">Nenhuma memória recuperada.</div>
          ) : (
            <div className="recovery-entries">
              {entries.map((e, i) => (
                <div key={i} className="recovery-entry">
                  <div className="recovery-entry-header">
                    <span className="recovery-idx">#{i + 1}</span>
                    <span className="recovery-source">{e.source}</span>
                    <span className="recovery-score">score={e.score.toFixed(4)}</span>
                    {e.session_id && <span className="recovery-sid">sess={e.session_id}…</span>}
                    {e.created_at && <span className="recovery-date">{e.created_at}</span>}
                  </div>
                  <div className="recovery-entry-content">
                    <CodeBlock content={e.content} maxLines={5} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Final / Error steps ───────────────────────────────────────────────

function FinalStep({ text, n }: { text: string; n: number }) {
  return (
    <div className="step-block step-final">
      <div className="step-header-static">
        <StepBadge n={n} color="#065f46" />
        <span className="step-icon">✅</span>
        <span className="step-title">Resposta Final</span>
      </div>
      <div className="step-body">
        <div className="final-text">{text}</div>
      </div>
    </div>
  )
}

function ErrorStep({ text, n }: { text: string; n: number }) {
  return (
    <div className="step-block step-error-block">
      <div className="step-header-static">
        <StepBadge n={n} color="#7f1d1d" />
        <span className="step-icon">❌</span>
        <span className="step-title">Erro</span>
      </div>
      <div className="step-body">
        <div className="error-text">{text}</div>
      </div>
    </div>
  )
}

// ── Trace card ────────────────────────────────────────────────────────

function TraceCard({
  trace, traceKey, open, onToggle, openSteps, onToggleStep,
}: {
  trace: MessageTrace
  traceKey: string
  open: boolean
  onToggle: () => void
  openSteps: Set<string>
  onToggleStep: (key: string) => void
}) {
  const intent = trace.intent_routing?.intent || 'UNKNOWN'
  const timeline = buildTimeline(trace)

  return (
    <div className="trace-card">
      <div className="trace-header" onClick={onToggle}>
        <span className="trace-role-icon">💬</span>
        <span className="trace-message" title={trace.user_message}>{trace.user_message}</span>
        <span className={`intent-badge intent-${intent}`}>{intent.replace(/_/g, ' ')}</span>
        <span className="step-count-badge">{timeline.length} passos</span>
        <span className="trace-duration">{fmtMs(trace.duration_ms)}</span>
        <span className="trace-time">{fmtTime(trace.started_at)}</span>
        <span className={`expand-chevron ${open ? 'open' : ''}`}>▼</span>
      </div>

      {open && (
        <div className="trace-timeline">
          {timeline.map((step, i) => {
            const key = `${traceKey}:step:${i}`
            const isOpen = openSteps.has(key)
            const toggle = () => onToggleStep(key)

            if (step.kind === 'topic_rotation') {
              return <TopicRotationStep key={key} step={step} n={i + 1} open={isOpen} onToggle={toggle} />
            }
            if (step.kind === 'intent') {
              return <IntentStep key={key} step={step} n={i + 1} open={isOpen} onToggle={toggle} />
            }
            if (step.kind === 'context_recovery') {
              return <ContextRecoveryStep key={key} step={step} n={i + 1} open={isOpen} onToggle={toggle} />
            }
            if (step.kind === 'tool') {
              return <ToolStep key={key} step={step} n={i + 1} open={isOpen} onToggle={toggle} />
            }
            if (step.kind === 'llm') {
              return <LLMStep key={key} step={step} n={i + 1} open={isOpen} onToggle={toggle} />
            }
            if (step.kind === 'final') {
              return <FinalStep key={key} text={step.text} n={i + 1} />
            }
            if (step.kind === 'error') {
              return <ErrorStep key={key} text={step.text} n={i + 1} />
            }
            return null
          })}

          <div className="trace-footer">
            Duração total: <span className="footer-dur">{fmtMs(trace.duration_ms)}</span>
            {trace.trace_id && (
              <span className="footer-id">ID: {trace.trace_id}</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Debug Panel ──────────────────────────────────────────────────

export default function DebugPanel() {
  const [sessions, setSessions] = useState<DebugSession[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [selectedSession, setSelectedSession] = useState<string | null>(null)
  const [traces, setTraces] = useState<MessageTrace[]>([])
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [autoRefresh, setAutoRefresh] = useState(true)

  // Lifted expand state — keyed stably so refresh doesn't collapse open cards
  const [openTraces, setOpenTraces] = useState<Set<string>>(new Set())
  const [openSteps, setOpenSteps] = useState<Set<string>>(new Set())

  const toggleTrace = useCallback((key: string) => {
    setOpenTraces(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }, [])

  const toggleStep = useCallback((key: string) => {
    setOpenSteps(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }, [])

  const loadSessions = useCallback(async () => {
    try {
      const [sessRes, statsRes] = await Promise.all([
        fetch(`${API}/sessions?limit=100`),
        fetch(`${API}/stats`),
      ])
      if (sessRes.ok) setSessions(await sessRes.json().then((d: { sessions?: DebugSession[] }) => d.sessions || []))
      if (statsRes.ok) setStats(await statsRes.json())
    } catch { /* silent */ }
  }, [])

  const loadTraces = useCallback(async (sessionId: string, silent = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await fetch(`${API}/sessions/${sessionId}`)
      if (res.ok) {
        const data = await res.json()
        setTraces(data.traces || [])
      }
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => { loadSessions() }, [loadSessions])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(() => {
      loadSessions()
      if (selectedSession) loadTraces(selectedSession, true)
    }, 5000)
    return () => clearInterval(id)
  }, [autoRefresh, selectedSession, loadSessions, loadTraces])

  const handleSelectSession = (id: string) => {
    setSelectedSession(id)
    loadTraces(id)
  }

  const filtered = sessions.filter(s =>
    s.session_id.includes(search) || (s.service || '').includes(search)
  )

  const selectedSessionObj = sessions.find(s => s.session_id === selectedSession)

  return (
    <div className="debug-panel">
      {/* Header */}
      <div className="debug-header">
        <div>
          <h1>🔬 Neo Debug</h1>
          <div className="subtitle">Observabilidade de sessões e mensagens</div>
        </div>
        {stats && stats.mongo_available && (
          <div className="stat-pill">
            <div><span>{stats.total_sessions}</span> sessões</div>
            <div><span>{stats.total_traces}</span> traces</div>
            <div>avg LLM <span>{stats.avg_llm_duration_ms ? fmtMs(stats.avg_llm_duration_ms) : '—'}</span></div>
          </div>
        )}
        {stats && !stats.mongo_available && (
          <div className="stat-pill" style={{ color: '#f87171' }}>⚠️ MongoDB offline</div>
        )}
        <button
          onClick={() => setAutoRefresh(a => !a)}
          className={`header-btn ${autoRefresh ? 'btn-active' : ''}`}
        >
          {autoRefresh ? '⏸ Auto-refresh ON' : '▶ Auto-refresh OFF'}
        </button>
        <button onClick={loadSessions} className="header-btn">
          🔄 Refresh
        </button>
      </div>

      <div className="debug-layout">
        {/* Sidebar */}
        <div className="sessions-sidebar">
          <div className="sidebar-search">
            <input
              placeholder="Buscar sessão..."
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
          </div>
          <div className="sessions-list">
            {filtered.length === 0 && (
              <div style={{ padding: 24, color: '#475569', fontSize: 13, textAlign: 'center' }}>
                Nenhuma sessão encontrada
              </div>
            )}
            {filtered.map(s => (
              <div
                key={s.session_id}
                className={`session-item ${selectedSession === s.session_id ? 'active' : ''}`}
                onClick={() => handleSelectSession(s.session_id)}
              >
                <div className="session-id">{s.session_id.slice(0, 8)}…{s.session_id.slice(-4)}</div>
                <div className="session-meta">
                  <span>{s.service || 'neo-desktop'}</span>
                  <span className={`status-badge status-${s.status}`}>{s.status}</span>
                </div>
                <div className="session-meta" style={{ marginTop: 4 }}>
                  <span>{fmtTime(s.last_activity_at)}</span>
                  <span className="session-count">{s.message_count} msgs</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Main */}
        <div className="debug-main">
          {!selectedSession ? (
            <div className="debug-empty">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" strokeWidth="1.5">
                <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2"/>
                <rect x="9" y="3" width="6" height="4" rx="1"/>
                <path d="M9 12h6M9 16h4"/>
              </svg>
              <div>Selecione uma sessão para ver os traces</div>
            </div>
          ) : (
            <>
              <div className="session-info-bar">
                <span className="session-info-id">{selectedSession}</span>
                {selectedSessionObj && (
                  <>
                    <span className="session-info-service">{selectedSessionObj.service}</span>
                    <span className={`status-badge status-${selectedSessionObj.status}`}>{selectedSessionObj.status}</span>
                    <span className="session-info-meta">Criada: {fmtTime(selectedSessionObj.created_at)}</span>
                    <span className="session-info-count">{traces.length} trace{traces.length !== 1 ? 's' : ''}</span>
                  </>
                )}
              </div>

              {loading ? (
                <div className="debug-empty">
                  <div style={{ color: '#7c3aed' }}>Carregando traces…</div>
                </div>
              ) : traces.length === 0 ? (
                <div className="debug-empty">
                  <div>Nenhum trace para esta sessão</div>
                </div>
              ) : (
                <div className="traces-area">
                  {traces.map((t, i) => {
                    const traceKey = t.trace_id || String(i)
                    return (
                      <TraceCard
                        key={traceKey}
                        trace={t}
                        traceKey={traceKey}
                        open={openTraces.has(traceKey)}
                        onToggle={() => toggleTrace(traceKey)}
                        openSteps={openSteps}
                        onToggleStep={toggleStep}
                      />
                    )
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
