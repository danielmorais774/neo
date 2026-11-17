import { useCallback, useMemo, useRef, useState } from 'react'

const NEO_VISION_API = 'http://localhost:5011'

// ── Types ──────────────────────────────────────────────────────

type ElementType = 'FACECAM' | 'SLIDER' | 'UI_INTERFACE' | 'VIDEO'
type Phase = 'idle' | 'uploading' | 'done' | 'error'

interface BBox { x1: number; y1: number; x2: number; y2: number }

interface DetectedElement {
  type: ElementType
  label: string
  bbox: BBox
  confidence: number
  metadata: Record<string, unknown>
}

interface AnalysisResult {
  width: number
  height: number
  scene_type: string
  elements: DetectedElement[]
  slide_text_md?: string
  layout_blocks?: LayoutBlock[]
  paddle_ocr_full?: PaddleOCRFull
  summary: {
    faces_detected: number
    has_slide: boolean
    has_ui: boolean
    is_fullscreen_facecam: boolean
  }
}

interface LayoutBlock {
  block_type: string
  bbox: BBox
  text: string
  confidence: number
  order: number
}

interface PaddleOCRLine {
  text: string
  bbox: [number, number, number, number]
  confidence: number
}

interface PaddleOCRFull {
  text: string
  lines: PaddleOCRLine[]
}

// ── Simple Markdown renderer ──────────────────────────────────

function SimpleMarkdown({ text }: { text: string }) {
  const rendered = useMemo(() => {
    const lines = text.split('\n')
    const elements: React.ReactNode[] = []
    let key = 0

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]

      // Horizontal rule
      if (/^---+$/.test(line.trim())) {
        elements.push(<hr key={key++} style={{ border: 'none', borderTop: '1px solid #2d3748', margin: '12px 0' }} />)
        continue
      }

      // Headers
      const hMatch = line.match(/^(#{1,4})\s+(.+)$/)
      if (hMatch) {
        const level = hMatch[1].length
        const sizes = [20, 17, 15, 13]
        const colors = ['#e2e8f0', '#a78bfa', '#448aff', '#94a3b8']
        elements.push(
          <div key={key++} style={{
            fontSize: sizes[level - 1] || 13,
            fontWeight: 700,
            color: colors[level - 1] || '#94a3b8',
            marginTop: level <= 2 ? 16 : 10,
            marginBottom: 6,
          }}>
            {inlineFormat(hMatch[2])}
          </div>
        )
        continue
      }

      // Blockquote
      if (line.startsWith('>')) {
        const content = line.replace(/^>\s*/, '')
        elements.push(
          <div key={key++} style={{
            borderLeft: '3px solid #7c3aed',
            paddingLeft: 12, margin: '6px 0',
            color: '#a78bfa', fontSize: 12, fontStyle: 'italic',
          }}>
            {inlineFormat(content)}
          </div>
        )
        continue
      }

      // Bullet list
      if (/^\s*[-*]\s+/.test(line)) {
        const content = line.replace(/^\s*[-*]\s+/, '')
        const indent = line.match(/^(\s*)/)?.[1]?.length || 0
        elements.push(
          <div key={key++} style={{
            display: 'flex', gap: 6, marginLeft: indent * 4,
            fontSize: 12, color: '#cbd5e1', lineHeight: 1.7,
          }}>
            <span style={{ color: '#64748b', flexShrink: 0 }}>•</span>
            <span>{inlineFormat(content)}</span>
          </div>
        )
        continue
      }

      // Empty line
      if (!line.trim()) {
        elements.push(<div key={key++} style={{ height: 8 }} />)
        continue
      }

      // Normal paragraph
      elements.push(
        <div key={key++} style={{ fontSize: 12, color: '#cbd5e1', lineHeight: 1.7, marginBottom: 2 }}>
          {inlineFormat(line)}
        </div>
      )
    }

    return elements
  }, [text])

  return <div>{rendered}</div>
}

function inlineFormat(text: string): React.ReactNode {
  // Process inline formatting: **bold**, *italic*, `code`, ==highlight==
  const parts: React.ReactNode[] = []
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|==(.+?)==)/g
  let lastIndex = 0
  let match: RegExpExecArray | null
  let k = 0

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index))
    }
    if (match[2]) {
      parts.push(<strong key={k++} style={{ color: '#e2e8f0', fontWeight: 700 }}>{match[2]}</strong>)
    } else if (match[3]) {
      parts.push(<em key={k++} style={{ color: '#a5b4fc' }}>{match[3]}</em>)
    } else if (match[4]) {
      parts.push(<code key={k++} style={{
        background: 'rgba(124,58,237,0.2)', color: '#c4b5fd',
        padding: '1px 5px', borderRadius: 3, fontSize: '0.9em',
      }}>{match[4]}</code>)
    } else if (match[5]) {
      parts.push(<mark key={k++} style={{
        background: 'rgba(250,204,21,0.25)', color: '#fde68a',
        padding: '0 3px', borderRadius: 2,
      }}>{match[5]}</mark>)
    }
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }
  return parts.length > 0 ? <>{parts}</> : text
}

// ── Colors ─────────────────────────────────────────────────────

const ELEMENT_COLORS: Record<string, string> = {
  FACECAM: '#00e676',
  SLIDER: '#448aff',
  UI_INTERFACE: '#ff9100',
  VIDEO: '#9e9e9e',
}

const BLOCK_TYPE_COLORS: Record<string, string> = {
  text: '#448aff',
  paragraph_title: '#fbbf24',
  title: '#fbbf24',
  list: '#34d399',
  table: '#f97316',
  table_title: '#fb923c',
  table_note: '#fdba74',
  figure: '#a78bfa',
  figure_title: '#818cf8',
  figure_caption: '#818cf8',
  image: '#a78bfa',
  header: '#22d3ee',
  footer: '#94a3b8',
  reference: '#78716c',
  equation: '#f472b6',
  abstract: '#e879f9',
  content: '#67e8f9',
  catalogue: '#67e8f9',
  seal: '#ef4444',
  unknown: '#64748b',
}

const BLOCK_TYPE_LABELS: Record<string, string> = {
  text: 'Texto',
  paragraph_title: 'Título',
  title: 'Título',
  list: 'Lista',
  table: 'Tabela',
  table_title: 'Título Tabela',
  table_note: 'Nota Tabela',
  figure: 'Figura',
  figure_title: 'Título Figura',
  figure_caption: 'Legenda',
  image: 'Imagem',
  header: 'Cabeçalho',
  footer: 'Rodapé',
  reference: 'Referência',
  equation: 'Equação',
  abstract: 'Resumo',
  content: 'Conteúdo',
  catalogue: 'Catálogo',
  seal: 'Selo',
}

const SCENE_LABELS: Record<string, string> = {
  facecam_fullscreen: '🎥 Facecam Fullscreen',
  slider_with_facecam_overlay: '📊 Slider + Facecam',
  slider_only: '📊 Slider Only',
  facecam_only: '🎥 Facecam Only',
  ui_interface: '🖥️ UI / Interface',
  video_normal: '🎬 Video Normal',
}

// ── Component ──────────────────────────────────────────────────

export default function NeoFrameAnalyzer() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [imageUrl, setImageUrl] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState('')
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [hoveredElement, setHoveredElement] = useState<number | null>(null)
  const [hoveredBlock, setHoveredBlock] = useState<number | null>(null)
  const [showOverlays, setShowOverlays] = useState(true)
  const [showLayoutOverlays, setShowLayoutOverlays] = useState(true)
  const [showSlideModal, setShowSlideModal] = useState(false)
  const [expandedBlock, setExpandedBlock] = useState<number | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const imgRef = useRef<HTMLImageElement>(null)

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setSelectedFile(file)
    setResult(null)
    setPhase('idle')
    setErrorMsg('')

    // Preview
    const url = URL.createObjectURL(file)
    setImageUrl(url)
  }, [])

  const handleAnalyze = useCallback(async () => {
    if (!selectedFile) return
    setPhase('uploading')
    setErrorMsg('')
    setResult(null)

    try {
      const fd = new FormData()
      fd.append('file', selectedFile)
      const res = await fetch(`${NEO_VISION_API}/api/analyze-frame`, { method: 'POST', body: fd })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'Unknown error' }))
        throw new Error(err.error || `HTTP ${res.status}`)
      }
      const data: AnalysisResult = await res.json()
      setResult(data)
      setPhase('done')
    } catch (err: any) {
      setErrorMsg(err.message || String(err))
      setPhase('error')
    }
  }, [selectedFile])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files[0]
    if (file && file.type.startsWith('image/')) {
      setSelectedFile(file)
      setResult(null)
      setPhase('idle')
      setErrorMsg('')
      setImageUrl(URL.createObjectURL(file))
    }
  }, [])

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        const file = item.getAsFile()
        if (file) {
          setSelectedFile(file)
          setResult(null)
          setPhase('idle')
          setErrorMsg('')
          setImageUrl(URL.createObjectURL(file))
        }
        break
      }
    }
  }, [])

  return (
    <div
      style={{ padding: 24, color: '#e2e8f0', minHeight: '100vh', fontFamily: 'Inter, system-ui, sans-serif' }}
      onDragOver={e => e.preventDefault()}
      onDrop={handleDrop}
      onPaste={handlePaste}
      tabIndex={0}
    >
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <span style={{ fontSize: 22, fontWeight: 700, color: '#7c3aed' }}>🔍 Frame Analyzer</span>
        <span style={{ fontSize: 13, color: '#64748b' }}>Upload an image to detect elements</span>
      </div>

      {/* Upload area */}
      <div
        style={{
          border: '2px dashed #2d3748',
          borderRadius: 12,
          padding: '24px 32px',
          textAlign: 'center',
          cursor: 'pointer',
          background: '#13161f',
          marginBottom: 20,
          transition: 'border-color 0.2s',
        }}
        onClick={() => fileInputRef.current?.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          onChange={handleFileSelect}
          style={{ display: 'none' }}
        />
        {selectedFile ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, justifyContent: 'center' }}>
            <span style={{ fontSize: 28 }}>🖼️</span>
            <div style={{ textAlign: 'left' }}>
              <div style={{ fontWeight: 600, color: '#e2e8f0' }}>{selectedFile.name}</div>
              <div style={{ fontSize: 12, color: '#64748b' }}>{(selectedFile.size / 1024).toFixed(0)} KB</div>
            </div>
            <button
              onClick={(e) => { e.stopPropagation(); handleAnalyze() }}
              disabled={phase === 'uploading'}
              style={{
                marginLeft: 20,
                padding: '8px 24px',
                background: phase === 'uploading' ? '#374151' : '#7c3aed',
                color: '#fff',
                border: 'none',
                borderRadius: 8,
                cursor: phase === 'uploading' ? 'wait' : 'pointer',
                fontWeight: 600,
                fontSize: 14,
              }}
            >
              {phase === 'uploading' ? '⏳ Analisando...' : '🔍 Analisar'}
            </button>
          </div>
        ) : (
          <div>
            <div style={{ fontSize: 36, marginBottom: 8 }}>📸</div>
            <div style={{ color: '#94a3b8', fontSize: 14 }}>
              Clique, arraste ou cole (Ctrl+V) uma imagem aqui
            </div>
          </div>
        )}
      </div>

      {/* Error message */}
      {errorMsg && (
        <div style={{ background: '#3b1a1a', color: '#f87171', padding: '10px 16px', borderRadius: 8, marginBottom: 16, fontSize: 13 }}>
          {errorMsg}
        </div>
      )}

      {/* Main content: Image + overlays | Sidebar with detections */}
      {imageUrl && (
        <div style={{ display: 'flex', gap: 20, alignItems: 'flex-start' }}>
          {/* Image with overlays */}
          <div style={{ flex: 1, position: 'relative', background: '#0a0c14', borderRadius: 12, overflow: 'hidden' }}>
            {/* Scene type badge */}
            {result && (
              <div style={{
                position: 'absolute', top: 12, left: 12, zIndex: 10,
                background: 'rgba(0,0,0,0.75)', padding: '6px 14px', borderRadius: 8,
                fontSize: 14, fontWeight: 600, color: '#e2e8f0', backdropFilter: 'blur(4px)',
              }}>
                {SCENE_LABELS[result.scene_type] || result.scene_type}
              </div>
            )}

            {/* Toggle overlays */}
            {result && (
              <div style={{ position: 'absolute', top: 12, right: 12, zIndex: 10, display: 'flex', gap: 6 }}>
                <button
                  onClick={() => setShowOverlays(v => !v)}
                  style={{
                    background: showOverlays ? 'rgba(124,58,237,0.8)' : 'rgba(0,0,0,0.6)',
                    color: '#fff', border: 'none', borderRadius: 6, padding: '5px 12px',
                    cursor: 'pointer', fontSize: 12, fontWeight: 600,
                  }}
                >
                  {showOverlays ? '👁️ Detecção' : '👁️‍🗨️ Detecção'}
                </button>
                {result.layout_blocks && result.layout_blocks.length > 0 && (
                  <button
                    onClick={() => setShowLayoutOverlays(v => !v)}
                    style={{
                      background: showLayoutOverlays ? 'rgba(68,138,255,0.8)' : 'rgba(0,0,0,0.6)',
                      color: '#fff', border: 'none', borderRadius: 6, padding: '5px 12px',
                      cursor: 'pointer', fontSize: 12, fontWeight: 600,
                    }}
                  >
                    {showLayoutOverlays ? '📐 Layout' : '📐 Layout'}
                  </button>
                )}
              </div>
            )}

            <div style={{ position: 'relative', display: 'inline-block', width: '100%' }}>
              <img
                ref={imgRef}
                src={imageUrl}
                alt="Frame"
                style={{ width: '100%', display: 'block' }}
                onLoad={() => {
                  // Force re-render for correct scale
                  if (result) setResult({ ...result })
                }}
              />

              {/* SVG overlay */}
              {result && showOverlays && imgRef.current && (
                <svg
                  style={{
                    position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
                    pointerEvents: 'none',
                  }}
                  viewBox={`0 0 ${result.width} ${result.height}`}
                  preserveAspectRatio="xMidYMid meet"
                >
                  {result.elements.map((el, i) => {
                    const color = ELEMENT_COLORS[el.type] || '#fff'
                    const isHovered = hoveredElement === i
                    const bw = el.bbox.x2 - el.bbox.x1
                    const bh = el.bbox.y2 - el.bbox.y1

                    // Don't render full-frame overlays for SLIDER/UI (too noisy)
                    const isFullFrame = bw >= result.width * 0.95 && bh >= result.height * 0.95
                    if (isFullFrame && (el.type === 'SLIDER' || el.type === 'UI_INTERFACE')) {
                      return (
                        <g key={i}>
                          {/* Just a corner badge */}
                          <rect
                            x={result.width - 200}
                            y={8}
                            width={190}
                            height={28}
                            rx={6}
                            fill={color}
                            fillOpacity={0.2}
                            stroke={color}
                            strokeWidth={1.5}
                          />
                          <text
                            x={result.width - 105}
                            y={27}
                            textAnchor="middle"
                            fill={color}
                            fontSize={14}
                            fontWeight={600}
                            fontFamily="Inter, system-ui, sans-serif"
                          >
                            {el.label}
                          </text>
                        </g>
                      )
                    }

                    return (
                      <g
                        key={i}
                        style={{ pointerEvents: 'all', cursor: 'pointer' }}
                        onMouseEnter={() => setHoveredElement(i)}
                        onMouseLeave={() => setHoveredElement(null)}
                      >
                        {/* Bbox rect */}
                        <rect
                          x={el.bbox.x1}
                          y={el.bbox.y1}
                          width={bw}
                          height={bh}
                          fill={color}
                          fillOpacity={isHovered ? 0.15 : 0.06}
                          stroke={color}
                          strokeWidth={isHovered ? 3 : 2}
                          strokeDasharray={el.type === 'FACECAM' ? 'none' : '6,3'}
                          rx={4}
                        />
                        {/* Label background */}
                        <rect
                          x={el.bbox.x1}
                          y={el.bbox.y1 - 26}
                          width={Math.max(bw, 120)}
                          height={24}
                          rx={4}
                          fill={color}
                          fillOpacity={0.85}
                        />
                        {/* Label text */}
                        <text
                          x={el.bbox.x1 + 6}
                          y={el.bbox.y1 - 8}
                          fill="#000"
                          fontSize={13}
                          fontWeight={700}
                          fontFamily="Inter, system-ui, sans-serif"
                        >
                          {el.label} ({(el.confidence * 100).toFixed(0)}%)
                        </text>
                      </g>
                    )
                  })}
                </svg>
              )}

              {/* Layout block SVG overlays */}
              {result && showLayoutOverlays && result.layout_blocks && imgRef.current && (
                <svg
                  style={{
                    position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
                    pointerEvents: 'none',
                  }}
                  viewBox={`0 0 ${result.width} ${result.height}`}
                  preserveAspectRatio="xMidYMid meet"
                >
                  {result.layout_blocks.map((block, i) => {
                    const color = BLOCK_TYPE_COLORS[block.block_type] || '#64748b'
                    const isHovered = hoveredBlock === i
                    const bw = block.bbox.x2 - block.bbox.x1
                    const bh = block.bbox.y2 - block.bbox.y1
                    return (
                      <g
                        key={`block-${i}`}
                        style={{ pointerEvents: 'all', cursor: 'pointer' }}
                        onMouseEnter={() => setHoveredBlock(i)}
                        onMouseLeave={() => setHoveredBlock(null)}
                      >
                        <rect
                          x={block.bbox.x1} y={block.bbox.y1}
                          width={bw} height={bh}
                          fill={color}
                          fillOpacity={isHovered ? 0.22 : 0.08}
                          stroke={color}
                          strokeWidth={isHovered ? 2.5 : 1.5}
                          strokeDasharray="5,3"
                          rx={4}
                        />
                        {/* Type label */}
                        <rect
                          x={block.bbox.x1} y={block.bbox.y1 - 20}
                          width={Math.min(bw, 100)} height={18}
                          rx={3} fill={color} fillOpacity={0.9}
                        />
                        <text
                          x={block.bbox.x1 + 4} y={block.bbox.y1 - 6}
                          fill="#000" fontSize={11} fontWeight={700}
                          fontFamily="Inter, system-ui, sans-serif"
                        >
                          {BLOCK_TYPE_LABELS[block.block_type] || block.block_type}
                        </text>
                      </g>
                    )
                  })}
                </svg>
              )}
            </div>
          </div>

          {/* Sidebar: Detected elements */}
          {result && (
            <div style={{ width: 300, flexShrink: 0 }}>
              {/* Summary card */}
              <div style={{
                background: '#13161f', borderRadius: 10, padding: 16, marginBottom: 14,
                border: '1px solid #1e2235',
              }}>
                <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 10, color: '#a78bfa' }}>
                  📋 Resumo
                </div>
                <div style={{ fontSize: 13, lineHeight: 1.8, color: '#94a3b8' }}>
                  <div>Resolução: <span style={{ color: '#e2e8f0' }}>{result.width} × {result.height}</span></div>
                  <div>Cena: <span style={{ color: '#e2e8f0' }}>{SCENE_LABELS[result.scene_type] || result.scene_type}</span></div>
                  <div>Faces: <span style={{ color: '#e2e8f0' }}>{result.summary.faces_detected}</span></div>
                  <div>Slide: <span style={{ color: '#e2e8f0' }}>{result.summary.has_slide ? 'Sim' : 'Não'}</span></div>
                  <div>UI: <span style={{ color: '#e2e8f0' }}>{result.summary.has_ui ? 'Sim' : 'Não'}</span></div>
                </div>
              </div>

              {/* ── Layout Analysis Panel ──────────────────────────────── */}
              {result.layout_blocks && result.layout_blocks.length > 0 && (
                <div style={{
                  background: 'linear-gradient(135deg, #13161f 0%, #0f1420 100%)',
                  borderRadius: 12, border: '1px solid #448aff33',
                  marginBottom: 14, overflow: 'hidden',
                }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '12px 16px', borderBottom: '1px solid #1e2235',
                    background: 'linear-gradient(90deg, rgba(68,138,255,0.10) 0%, transparent 100%)',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontSize: 16 }}>📐</span>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: 13, color: '#448aff' }}>
                          Layout Analysis ({result.layout_blocks.length} blocos)
                        </div>
                        <div style={{ fontSize: 10, color: '#64748b', marginTop: 1 }}>PPStructure + PaddleOCR</div>
                      </div>
                    </div>
                    <button
                      onClick={() => {
                        const allText = result.layout_blocks!.map(b => `[${b.block_type.toUpperCase()}]\n${b.text}`).join('\n\n')
                        navigator.clipboard.writeText(allText)
                      }}
                      title="Copiar tudo"
                      style={{
                        padding: '4px 10px', fontSize: 11,
                        background: 'rgba(68,138,255,0.1)', color: '#448aff',
                        border: '1px solid #448aff33', borderRadius: 6,
                        cursor: 'pointer', fontWeight: 600,
                      }}
                    >📋</button>
                  </div>

                  <div style={{ padding: '8px 12px', maxHeight: 400, overflowY: 'auto' }}>
                    {result.layout_blocks.map((block, i) => {
                      const color = BLOCK_TYPE_COLORS[block.block_type] || '#64748b'
                      const label = BLOCK_TYPE_LABELS[block.block_type] || block.block_type
                      const isHovered = hoveredBlock === i
                      const isExpanded = expandedBlock === i
                      return (
                        <div
                          key={i}
                          style={{
                            marginBottom: 6, borderRadius: 8,
                            border: `1px solid ${isHovered ? color + '66' : '#1e2235'}`,
                            background: isHovered ? `${color}11` : '#0d1017',
                            transition: 'all 0.15s', overflow: 'hidden',
                          }}
                          onMouseEnter={() => setHoveredBlock(i)}
                          onMouseLeave={() => setHoveredBlock(null)}
                        >
                          {/* Block header */}
                          <div
                            onClick={() => setExpandedBlock(isExpanded ? null : i)}
                            style={{
                              display: 'flex', alignItems: 'center', gap: 8,
                              padding: '8px 10px', cursor: 'pointer',
                            }}
                          >
                            <div style={{
                              padding: '2px 8px', borderRadius: 4, fontSize: 10,
                              fontWeight: 700, background: color + '22', color,
                              textTransform: 'uppercase', whiteSpace: 'nowrap',
                            }}>
                              {label}
                            </div>
                            <div style={{
                              flex: 1, fontSize: 12, color: '#94a3b8',
                              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            }}>
                              {block.text ? block.text.split('\n')[0].slice(0, 50) : '(sem texto)'}
                            </div>
                            {block.confidence > 0 && (
                              <div style={{ fontSize: 10, color: '#475569', whiteSpace: 'nowrap' }}>
                                {(block.confidence * 100).toFixed(0)}%
                              </div>
                            )}
                            <span style={{ fontSize: 10, color: '#475569', transition: 'transform 0.15s', transform: isExpanded ? 'rotate(90deg)' : 'none' }}>▶</span>
                          </div>

                          {/* Block content (expanded) */}
                          {isExpanded && block.text && (
                            <div style={{
                              padding: '8px 12px', borderTop: `1px solid ${color}22`,
                              background: '#080a10',
                            }}>
                              <pre style={{
                                margin: 0, fontSize: 12, color: '#cbd5e1',
                                whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                                lineHeight: 1.6, fontFamily: 'Inter, system-ui, sans-serif',
                              }}>
                                {block.text}
                              </pre>
                              <div style={{ marginTop: 6, fontSize: 10, color: '#475569' }}>
                                ({block.bbox.x1},{block.bbox.y1}) → ({block.bbox.x2},{block.bbox.y2})
                              </div>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* ── PaddleOCR Full Frame Panel ─────────────────────────── */}
              {result.paddle_ocr_full && result.paddle_ocr_full.text && (
                <div style={{
                  background: 'linear-gradient(135deg, #13161f 0%, #0f1420 100%)',
                  borderRadius: 12, border: '1px solid #34d39944',
                  marginBottom: 14, overflow: 'hidden',
                }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '12px 16px', borderBottom: '1px solid #1e2235',
                    background: 'linear-gradient(90deg, rgba(52,211,153,0.08) 0%, transparent 100%)',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontSize: 16 }}>🔍</span>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: 13, color: '#34d399' }}>
                          PaddleOCR Full Frame
                        </div>
                        <div style={{ fontSize: 10, color: '#64748b', marginTop: 1 }}>
                          {result.paddle_ocr_full.lines.length} linhas detectadas
                        </div>
                      </div>
                    </div>
                    <button
                      onClick={() => navigator.clipboard.writeText(result.paddle_ocr_full!.text)}
                      title="Copiar"
                      style={{
                        padding: '4px 10px', fontSize: 11,
                        background: 'rgba(52,211,153,0.1)', color: '#34d399',
                        border: '1px solid #34d39933', borderRadius: 6,
                        cursor: 'pointer', fontWeight: 600,
                      }}
                    >📋</button>
                  </div>

                  <div style={{ padding: '14px 16px', maxHeight: 260, overflowY: 'auto' }}>
                    <pre style={{
                      margin: 0, fontSize: 12, color: '#cbd5e1',
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                      lineHeight: 1.7, fontFamily: 'Inter, system-ui, sans-serif',
                    }}>
                      {result.paddle_ocr_full.text}
                    </pre>
                  </div>

                  {/* Expandable lines detail */}
                  <details style={{ borderTop: '1px solid #1e2235' }}>
                    <summary style={{
                      color: '#475569', fontSize: 11, cursor: 'pointer',
                      padding: '8px 16px', userSelect: 'none',
                      listStyle: 'none', display: 'flex', alignItems: 'center', gap: 6,
                    }}>
                      <span>▶</span> Linhas individuais ({result.paddle_ocr_full.lines.length})
                    </summary>
                    <div style={{ padding: '8px 12px', background: '#080a10', maxHeight: 200, overflowY: 'auto' }}>
                      {result.paddle_ocr_full.lines.map((ln, i) => (
                        <div key={i} style={{
                          padding: '4px 8px', marginBottom: 3, borderRadius: 4,
                          background: '#0d1017', fontSize: 11,
                          display: 'flex', gap: 8, alignItems: 'baseline',
                        }}>
                          <span style={{
                            color: ln.confidence >= 0.9 ? '#34d399' : ln.confidence >= 0.7 ? '#fbbf24' : '#f87171',
                            fontWeight: 700, fontSize: 10, whiteSpace: 'nowrap',
                          }}>
                            {(ln.confidence * 100).toFixed(0)}%
                          </span>
                          <span style={{ color: '#cbd5e1', wordBreak: 'break-word' }}>{ln.text}</span>
                        </div>
                      ))}
                    </div>
                  </details>
                </div>
              )}

              {/* ── AI Interpretation Panel (GPT-4o-mini) ──────────────── */}
              {result.slide_text_md && (
                <div style={{
                  background: 'linear-gradient(135deg, #13161f 0%, #0f1420 100%)',
                  borderRadius: 12, border: '1px solid #7c3aed44',
                  marginBottom: 14, overflow: 'hidden',
                }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '12px 16px',
                    borderBottom: '1px solid #1e2235',
                    background: 'linear-gradient(90deg, rgba(124,58,237,0.10) 0%, transparent 100%)',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontSize: 16 }}>✨</span>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: 13, color: '#a78bfa' }}>Interpretação IA</div>
                        <div style={{ fontSize: 10, color: '#64748b', marginTop: 1 }}>GPT-4o-mini Vision</div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button
                        onClick={() => navigator.clipboard.writeText(result.slide_text_md || '')}
                        title="Copiar"
                        style={{
                          padding: '4px 10px', fontSize: 11,
                          background: 'rgba(124,58,237,0.1)', color: '#a78bfa',
                          border: '1px solid #7c3aed33', borderRadius: 6,
                          cursor: 'pointer', fontWeight: 600,
                        }}
                      >📋</button>
                      <button
                        onClick={() => setShowSlideModal(true)}
                        title="Expandir"
                        style={{
                          padding: '4px 10px', fontSize: 11,
                          background: 'rgba(124,58,237,0.15)', color: '#a78bfa',
                          border: '1px solid #7c3aed44', borderRadius: 6,
                          cursor: 'pointer', fontWeight: 600,
                        }}
                      >⛶</button>
                    </div>
                  </div>
                  <div style={{ padding: '14px 16px', maxHeight: 280, overflowY: 'auto' }}>
                    <SimpleMarkdown text={result.slide_text_md} />
                  </div>
                </div>
              )}

              {/* Elements list */}
              <div style={{
                background: '#13161f', borderRadius: 10, padding: 16,
                border: '1px solid #1e2235',
              }}>
                <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 10, color: '#a78bfa' }}>
                  🧩 Elementos ({result.elements.length})
                </div>
                {result.elements.length === 0 && (
                  <div style={{ color: '#64748b', fontSize: 13 }}>Nenhum elemento detectado</div>
                )}
                {result.elements.map((el, i) => {
                  const color = ELEMENT_COLORS[el.type] || '#fff'
                  const isHovered = hoveredElement === i
                  return (
                    <div
                      key={i}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 10,
                        padding: '8px 10px', borderRadius: 8, marginBottom: 6,
                        background: isHovered ? 'rgba(124,58,237,0.15)' : 'rgba(255,255,255,0.03)',
                        cursor: 'pointer', transition: 'background 0.15s',
                        border: isHovered ? `1px solid ${color}44` : '1px solid transparent',
                      }}
                      onMouseEnter={() => setHoveredElement(i)}
                      onMouseLeave={() => setHoveredElement(null)}
                    >
                      <div style={{
                        width: 10, height: 10, borderRadius: '50%',
                        background: color, flexShrink: 0,
                      }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, color: '#e2e8f0' }}>
                          {el.label}
                        </div>
                        <div style={{ fontSize: 11, color: '#64748b' }}>
                          {el.type} · {(el.confidence * 100).toFixed(0)}%
                          {el.bbox && ` · (${el.bbox.x1},${el.bbox.y1})→(${el.bbox.x2},${el.bbox.y2})`}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* Raw JSON toggle */}
              <details style={{ marginTop: 14 }}>
                <summary style={{ color: '#64748b', fontSize: 12, cursor: 'pointer' }}>
                  🔧 JSON bruto
                </summary>
                <pre style={{
                  background: '#0a0c14', color: '#94a3b8', padding: 12, borderRadius: 8,
                  fontSize: 11, overflow: 'auto', maxHeight: 300, marginTop: 8,
                  border: '1px solid #1e2235',
                }}>
                  {JSON.stringify(result, null, 2)}
                </pre>
              </details>
            </div>
          )}
        </div>
      )}
      {/* AI Interpretation modal */}
      {showSlideModal && result && result.slide_text_md && (
        <div
          onClick={() => setShowSlideModal(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0,0,0,0.85)',
            backdropFilter: 'blur(6px)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 32,
          }}
        >
          <div
            onClick={e => e.stopPropagation()}
            style={{
              background: '#0f1117',
              border: '1px solid #7c3aed55',
              borderRadius: 16,
              width: '100%', maxWidth: 820,
              maxHeight: '90vh',
              display: 'flex', flexDirection: 'column',
              boxShadow: '0 24px 80px rgba(124,58,237,0.15)',
            }}
          >
            {/* Modal header */}
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '16px 24px',
              borderBottom: '1px solid #1e2235',
              background: 'linear-gradient(90deg, rgba(124,58,237,0.1) 0%, transparent 100%)',
              borderRadius: '16px 16px 0 0',
              flexShrink: 0,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontSize: 20 }}>✨</span>
                <div>
                  <div style={{ fontWeight: 700, fontSize: 15, color: '#e2e8f0' }}>Interpretação IA</div>
                  <div style={{ fontSize: 11, color: '#64748b' }}>GPT-4o-mini Vision · {result.scene_type}</div>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  onClick={() => navigator.clipboard.writeText(result.slide_text_md || '')}
                  style={{
                    padding: '6px 16px', fontSize: 12,
                    background: 'rgba(124,58,237,0.15)', color: '#a78bfa',
                    border: '1px solid #7c3aed44', borderRadius: 8,
                    cursor: 'pointer', fontWeight: 600,
                  }}
                >
                  📋 Copiar
                </button>
                <button
                  onClick={() => setShowSlideModal(false)}
                  style={{
                    padding: '6px 14px', fontSize: 18, lineHeight: 1,
                    background: 'rgba(255,255,255,0.05)', color: '#94a3b8',
                    border: '1px solid #2d3748', borderRadius: 8,
                    cursor: 'pointer',
                  }}
                >
                  ✕
                </button>
              </div>
            </div>

            {/* Modal body */}
            <div style={{
              padding: '24px 32px',
              overflowY: 'auto',
              flex: 1,
            }}>
              <SimpleMarkdown text={result.slide_text_md} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}