import { useEffect, useRef, useState, useCallback } from 'react'

const API = 'http://192.168.4.175:8000/api/v1/knowledge-graph'

// ── Cores por tipo de entidade ────────────────────────────────────────────────
const TYPE_COLORS: Record<string, string> = {
  perfil:      '#818cf8',
  saude:       '#34d399',
  restricao:   '#f87171',
  rotina:      '#fbbf24',
  contexto:    '#60a5fa',
  refeicao:    '#f472b6',
  treino:      '#a78bfa',
  suplemento:  '#22d3ee',
  objetivo:    '#fb923c',
  alimento:    '#a3e635',
  preferencia: '#2dd4bf',
  exercicio:   '#c084fc',
}
const DEFAULT_COLOR = '#94a3b8'

function typeColor(type: string) {
  return TYPE_COLORS[type] ?? DEFAULT_COLOR
}

// ── Tipos ─────────────────────────────────────────────────────────────────────
interface Entity {
  id: string
  type: string
  name: string
  user_id: string | null
  properties: Record<string, unknown>
  created_at: string
}

interface Relation {
  id: string
  source_id: string
  target_id: string
  type: string
  user_id: string | null
  properties: Record<string, unknown>
}

interface GraphData {
  entities: Entity[]
  relations: Relation[]
}

interface NodePos {
  id: string
  x: number
  y: number
  vx: number
  vy: number
}

// ── Force-directed layout ─────────────────────────────────────────────────────
function runForceLayout(
  entities: Entity[],
  relations: Relation[],
  width: number,
  height: number,
): Map<string, { x: number; y: number }> {
  const nodes: NodePos[] = entities.map((e, i) => ({
    id: e.id,
    x: width / 2 + Math.cos((i / entities.length) * Math.PI * 2) * 200,
    y: height / 2 + Math.sin((i / entities.length) * Math.PI * 2) * 200,
    vx: 0,
    vy: 0,
  }))

  const nodeMap = new Map<string, NodePos>(nodes.map(n => [n.id, n]))

  const REPULSION = 3500
  const SPRING_LENGTH = 150
  const SPRING_K = 0.04
  const DAMPING = 0.85
  const CENTER_GRAVITY = 0.015
  const ITERATIONS = 400

  for (let iter = 0; iter < ITERATIONS; iter++) {
    // Repulsão entre todos os nós
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j]
        const dx = b.x - a.x
        const dy = b.y - a.y
        const dist = Math.sqrt(dx * dx + dy * dy) || 1
        const force = REPULSION / (dist * dist)
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        a.vx -= fx; a.vy -= fy
        b.vx += fx; b.vy += fy
      }
    }

    // Molas pelas arestas
    for (const rel of relations) {
      const a = nodeMap.get(rel.source_id)
      const b = nodeMap.get(rel.target_id)
      if (!a || !b) continue
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 1
      const stretch = dist - SPRING_LENGTH
      const fx = (dx / dist) * stretch * SPRING_K
      const fy = (dy / dist) * stretch * SPRING_K
      a.vx += fx; a.vy += fy
      b.vx -= fx; b.vy -= fy
    }

    // Gravidade para o centro
    for (const n of nodes) {
      n.vx += (width / 2 - n.x) * CENTER_GRAVITY
      n.vy += (height / 2 - n.y) * CENTER_GRAVITY
    }

    // Integração + amortecimento
    for (const n of nodes) {
      n.vx *= DAMPING
      n.vy *= DAMPING
      n.x += n.vx
      n.y += n.vy
    }
  }

  const result = new Map<string, { x: number; y: number }>()
  for (const n of nodes) result.set(n.id, { x: n.x, y: n.y })
  return result
}

// ── Componente principal ──────────────────────────────────────────────────────
export default function KnowledgeGraphPanel() {
  const [graphData, setGraphData] = useState<GraphData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [userIds, setUserIds] = useState<string[]>([])
  const [selectedUser, setSelectedUser] = useState<string>('')
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }>>(new Map())
  const [tooltip, setTooltip] = useState<{ entity: Entity; x: number; y: number } | null>(null)

  // Pan + zoom
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 })
  const isPanning = useRef(false)
  const panStart = useRef({ x: 0, y: 0, tx: 0, ty: 0 })

  // Drag de nó
  const draggingNode = useRef<string | null>(null)
  const dragOffset = useRef({ x: 0, y: 0 })
  const posRef = useRef<Map<string, { x: number; y: number }>>(new Map())

  const svgRef = useRef<SVGSVGElement>(null)
  const SVG_W = 1200
  const SVG_H = 800

  // ── Fetch users ───────────────────────────────────────────────
  const fetchUsers = useCallback(async () => {
    try {
      const res = await fetch(`${API}/users`)
      const data = await res.json()
      setUserIds(data.user_ids ?? [])
    } catch {
      // sem usuários no grafo ainda
    }
  }, [])

  // ── Fetch graph ───────────────────────────────────────────────
  const fetchGraph = useCallback(async (uid?: string) => {
    setLoading(true)
    setError(null)
    try {
      const url = uid ? `${API}/graph?user_id=${uid}` : `${API}/graph`
      const res = await fetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: GraphData = await res.json()
      setGraphData(data)
      const pos = runForceLayout(data.entities, data.relations, SVG_W, SVG_H)
      posRef.current = pos
      setPositions(new Map(pos))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Erro ao buscar grafo')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchUsers()
    fetchGraph()
  }, [fetchUsers, fetchGraph])

  // ── Zoom via scroll ───────────────────────────────────────────
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    const factor = e.deltaY < 0 ? 1.1 : 0.91
    setTransform(t => ({ ...t, scale: Math.min(4, Math.max(0.15, t.scale * factor)) }))
  }, [])

  // ── Pan no fundo ──────────────────────────────────────────────
  const handleSvgMouseDown = useCallback((e: React.MouseEvent) => {
    if ((e.target as SVGElement).dataset.node) return
    isPanning.current = true
    panStart.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y }
  }, [transform])

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (draggingNode.current) {
      const svgEl = svgRef.current
      if (!svgEl) return
      const rect = svgEl.getBoundingClientRect()
      const svgX = (e.clientX - rect.left - transform.x) / transform.scale
      const svgY = (e.clientY - rect.top - transform.y) / transform.scale
      const nx = svgX - dragOffset.current.x
      const ny = svgY - dragOffset.current.y
      posRef.current.set(draggingNode.current, { x: nx, y: ny })
      setPositions(new Map(posRef.current))
      return
    }
    if (isPanning.current) {
      const dx = e.clientX - panStart.current.x
      const dy = e.clientY - panStart.current.y
      setTransform(t => ({ ...t, x: panStart.current.tx + dx, y: panStart.current.ty + dy }))
    }
  }, [transform])

  const handleMouseUp = useCallback(() => {
    isPanning.current = false
    draggingNode.current = null
  }, [])

  // ── Drag de nó ────────────────────────────────────────────────
  const handleNodeMouseDown = useCallback((e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    const svgEl = svgRef.current
    if (!svgEl) return
    const rect = svgEl.getBoundingClientRect()
    const svgX = (e.clientX - rect.left - transform.x) / transform.scale
    const svgY = (e.clientY - rect.top - transform.y) / transform.scale
    const pos = posRef.current.get(id)
    if (!pos) return
    draggingNode.current = id
    dragOffset.current = { x: svgX - pos.x, y: svgY - pos.y }
    setTooltip(null)
  }, [transform])

  // ── Tooltip ───────────────────────────────────────────────────
  const handleNodeClick = useCallback((e: React.MouseEvent, entity: Entity) => {
    e.stopPropagation()
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return
    setTooltip({ entity, x: e.clientX - rect.left, y: e.clientY - rect.top })
  }, [])

  // ── Render ────────────────────────────────────────────────────
  const entities = graphData?.entities ?? []
  const relations = graphData?.relations ?? []

  // tipos únicos presentes no grafo atual
  const types = [...new Set(entities.map(e => e.type))].sort()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, background: '#0f1117', color: '#e2e8f0' }}>
      {/* ── Toolbar ── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12,
        padding: '10px 20px', borderBottom: '1px solid #1e2235', flexShrink: 0,
      }}>
        <span style={{ color: '#7c3aed', fontWeight: 700 }}>Knowledge Graph</span>

        <select
          value={selectedUser}
          onChange={e => { setSelectedUser(e.target.value); fetchGraph(e.target.value || undefined) }}
          style={{
            background: '#1e2235', color: '#e2e8f0', border: '1px solid #334155',
            borderRadius: 6, padding: '4px 10px', fontSize: 13,
          }}
        >
          <option value="">Todos os usuários</option>
          {userIds.map(uid => (
            <option key={uid} value={uid}>{uid.slice(0, 8)}…</option>
          ))}
        </select>

        <button
          onClick={() => fetchGraph(selectedUser || undefined)}
          style={{
            background: '#7c3aed', color: '#fff', border: 'none',
            borderRadius: 6, padding: '5px 14px', cursor: 'pointer', fontSize: 13,
          }}
        >
          Atualizar
        </button>

        <span style={{ color: '#64748b', fontSize: 13 }}>
          {entities.length} entidades · {relations.length} relações
        </span>

        <span style={{ color: '#64748b', fontSize: 12, marginLeft: 'auto' }}>
          Scroll para zoom · Arraste o fundo para mover · Arraste nós para reposicionar
        </span>
      </div>

      {/* ── Legenda ── */}
      {types.length > 0 && (
        <div style={{
          display: 'flex', flexWrap: 'wrap', gap: 8,
          padding: '8px 20px', borderBottom: '1px solid #1e2235', flexShrink: 0,
        }}>
          {types.map(t => (
            <span key={t} style={{
              display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: '#cbd5e1',
            }}>
              <span style={{
                width: 10, height: 10, borderRadius: '50%',
                background: typeColor(t), display: 'inline-block',
              }} />
              {t}
            </span>
          ))}
        </div>
      )}

      {/* ── Estado: loading / error / vazio ── */}
      {loading && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#7c3aed' }}>
          Carregando grafo…
        </div>
      )}
      {!loading && error && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#f87171' }}>
          {error}
        </div>
      )}
      {!loading && !error && entities.length === 0 && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#475569' }}>
          Nenhuma entidade encontrada no grafo de conhecimento.
        </div>
      )}

      {/* ── SVG Graph ── */}
      {!loading && !error && entities.length > 0 && (
        <div style={{ flex: 1, overflow: 'hidden', position: 'relative', cursor: isPanning.current ? 'grabbing' : 'grab' }}
          onClick={() => setTooltip(null)}
        >
          <svg
            ref={svgRef}
            width="100%"
            height="100%"
            onWheel={handleWheel}
            onMouseDown={handleSvgMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
            style={{ display: 'block' }}
          >
            <g transform={`translate(${transform.x},${transform.y}) scale(${transform.scale})`}>
              {/* Definição de seta */}
              <defs>
                <marker id="arrow" markerWidth="8" markerHeight="8" refX="16" refY="3" orient="auto">
                  <path d="M0,0 L0,6 L8,3 z" fill="#475569" />
                </marker>
              </defs>

              {/* ── Arestas ── */}
              {relations.map(rel => {
                const src = positions.get(rel.source_id)
                const tgt = positions.get(rel.target_id)
                if (!src || !tgt) return null
                const mx = (src.x + tgt.x) / 2
                const my = (src.y + tgt.y) / 2
                return (
                  <g key={rel.id}>
                    <line
                      x1={src.x} y1={src.y} x2={tgt.x} y2={tgt.y}
                      stroke="#334155" strokeWidth={1.5}
                      markerEnd="url(#arrow)"
                    />
                    <text
                      x={mx} y={my - 4}
                      textAnchor="middle"
                      fill="#64748b"
                      fontSize={9}
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {rel.type}
                    </text>
                  </g>
                )
              })}

              {/* ── Nós ── */}
              {entities.map(entity => {
                const pos = positions.get(entity.id)
                if (!pos) return null
                const color = typeColor(entity.type)
                const r = 22
                return (
                  <g
                    key={entity.id}
                    transform={`translate(${pos.x},${pos.y})`}
                    style={{ cursor: 'pointer' }}
                    data-node={entity.id}
                    onMouseDown={e => handleNodeMouseDown(e, entity.id)}
                    onClick={e => handleNodeClick(e, entity)}
                  >
                    {/* Halo / glow */}
                    <circle r={r + 5} fill={color} opacity={0.12} />
                    {/* Círculo principal */}
                    <circle
                      r={r}
                      fill={color}
                      stroke="#0f1117"
                      strokeWidth={2}
                      data-node={entity.id}
                    />
                    {/* Ícone de tipo (1ª letra) */}
                    <text
                      textAnchor="middle"
                      dominantBaseline="central"
                      fill="#0f1117"
                      fontSize={11}
                      fontWeight={700}
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {entity.type.slice(0, 2).toUpperCase()}
                    </text>
                    {/* Label abaixo */}
                    <text
                      y={r + 12}
                      textAnchor="middle"
                      fill="#e2e8f0"
                      fontSize={10}
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {entity.name.length > 18 ? entity.name.slice(0, 16) + '…' : entity.name}
                    </text>
                  </g>
                )
              })}
            </g>
          </svg>

          {/* ── Tooltip ── */}
          {tooltip && (
            <div
              style={{
                position: 'absolute',
                left: Math.min(tooltip.x + 14, window.innerWidth - 260),
                top: tooltip.y + 14,
                background: '#1e2235',
                border: `1px solid ${typeColor(tooltip.entity.type)}`,
                borderRadius: 8,
                padding: '10px 14px',
                minWidth: 220,
                maxWidth: 320,
                boxShadow: '0 4px 20px rgba(0,0,0,0.5)',
                zIndex: 100,
                pointerEvents: 'none',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span style={{
                  width: 10, height: 10, borderRadius: '50%',
                  background: typeColor(tooltip.entity.type), flexShrink: 0,
                }} />
                <span style={{ color: '#e2e8f0', fontWeight: 700, fontSize: 14 }}>
                  {tooltip.entity.name}
                </span>
              </div>
              <div style={{ color: '#94a3b8', fontSize: 12, marginBottom: 4 }}>
                <b>Tipo:</b> {tooltip.entity.type}
              </div>
              {tooltip.entity.user_id && (
                <div style={{ color: '#94a3b8', fontSize: 12, marginBottom: 4 }}>
                  <b>Usuário:</b> {tooltip.entity.user_id.slice(0, 8)}…
                </div>
              )}
              <div style={{ color: '#94a3b8', fontSize: 12, marginBottom: 4 }}>
                <b>Criado:</b> {new Date(tooltip.entity.created_at).toLocaleString('pt-BR')}
              </div>
              {Object.keys(tooltip.entity.properties).length > 0 && (
                <div style={{ marginTop: 6 }}>
                  <div style={{ color: '#7c3aed', fontSize: 11, fontWeight: 600, marginBottom: 4 }}>Propriedades</div>
                  {Object.entries(tooltip.entity.properties).map(([k, v]) => (
                    <div key={k} style={{ color: '#94a3b8', fontSize: 11, marginBottom: 2 }}>
                      <b style={{ color: '#cbd5e1' }}>{k}:</b>{' '}
                      {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                    </div>
                  ))}
                </div>
              )}
              <div style={{ color: '#475569', fontSize: 10, marginTop: 6, wordBreak: 'break-all' }}>
                {tooltip.entity.id}
              </div>
            </div>
          )}

          {/* ── Controles de zoom ── */}
          <div style={{
            position: 'absolute', bottom: 16, right: 16,
            display: 'flex', flexDirection: 'column', gap: 4,
          }}>
            {[
              { label: '+', fn: () => setTransform(t => ({ ...t, scale: Math.min(4, t.scale * 1.2) })) },
              { label: '−', fn: () => setTransform(t => ({ ...t, scale: Math.max(0.15, t.scale * 0.83) })) },
              { label: '⌂', fn: () => setTransform({ x: 0, y: 0, scale: 1 }) },
            ].map(({ label, fn }) => (
              <button
                key={label}
                onClick={fn}
                style={{
                  width: 32, height: 32, background: '#1e2235', color: '#e2e8f0',
                  border: '1px solid #334155', borderRadius: 6, cursor: 'pointer',
                  fontSize: 16, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
