import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const NEO_VISION_API = 'http://localhost:5011'

// ── Types ─────────────────────────────────────────────────────────────────────

type SceneType =
  | 'video_normal'
  | 'slider_only'
  | 'facecam_only'
  | 'facecam_fullscreen'
  | 'slider_with_facecam_overlay'
  | 'ui_interface'
  | 'ui_with_facecam'
  | 'mixed_elements'

type ActivityClass = 'TALKING' | 'GESTURING' | 'STILL' | 'ABSENT'
type ElementType = 'FACECAM' | 'SLIDER' | 'UI_INTERFACE' | 'VIDEO'
type Phase = 'idle' | 'uploading' | 'analyzing' | 'done' | 'error'

interface BBox { x1: number; y1: number; x2: number; y2: number }

interface DetectedElement {
  type: ElementType
  bbox: BBox
  confidence: number
  metadata: Record<string, unknown>
}

interface PoseLandmark { x: number; y: number; visibility: number }

interface PoseData {
  landmarks: PoseLandmark[]
  connections: [number, number][]
  activity_class: ActivityClass
  energy: number
  wrist_velocity: number
}

interface RawDetection {
  facecam_type: string
  facecam_confidence: number
  facecam_face_count: number
  slide_number: number
  scene: string
  has_slide_content: boolean
}

interface FrameResult {
  frame_id: number
  timestamp_ms: number
  scene_type: SceneType
  resolution: { width: number; height: number }
  elements: DetectedElement[]
  pose?: PoseData
  raw?: RawDetection
  slide_text?: string
  slide_text_id?: number
}

interface SlideTextEntry {
  slide_id: number
  text: string
  frame_count: number
  first_timestamp_ms: number
  last_timestamp_ms: number
}

interface TimelineSegment {
  segment_id: number
  start_ms: number
  end_ms: number
  scene_type: SceneType
  dominant_elements: string[]
  note?: string
  slide_texts?: SlideTextEntry[]
}

interface FaceTimelineEntry {
  timestamp_ms: number
  facecam_type: string
  activity_class: ActivityClass
  energy: number
}

interface VideoInfo {
  width: number
  height: number
  fps: number
  duration_ms: number
  total_frames: number
  frames_analyzed: number
}

interface AIKeyframe {
  frame_idx: number
  timestamp_ms: number
  scene_type: string
  ocr_text: string
  text_diff_ratio: number
  slide_id: number
  interpreted: boolean
  slide_text_md: string | null
}

// ── Constants ─────────────────────────────────────────────────────────────────

const SCENE_COLORS: Record<SceneType, string> = {
  video_normal: '#3b82f6',
  slider_only: '#22c55e',
  facecam_only: '#f97316',
  facecam_fullscreen: '#ef4444',
  slider_with_facecam_overlay: '#a855f7',
  ui_interface: '#06b6d4',
  ui_with_facecam: '#eab308',
  mixed_elements: '#ec4899',
}

const SCENE_LABELS: Record<SceneType, string> = {
  video_normal: 'Video Normal',
  slider_only: 'Slide',
  facecam_only: 'Facecam',
  facecam_fullscreen: 'Facecam Full',
  slider_with_facecam_overlay: 'Slide + Facecam',
  ui_interface: 'UI/Interface',
  ui_with_facecam: 'UI + Facecam',
  mixed_elements: 'Mixed',
}

const ELEMENT_COLORS: Record<ElementType, string> = {
  FACECAM: '#f97316',
  SLIDER: '#a855f7',
  UI_INTERFACE: '#06b6d4',
  VIDEO: '#3b82f6',
}

const ACTIVITY_COLORS: Record<ActivityClass, string> = {
  TALKING: '#22c55e',
  GESTURING: '#f59e0b',
  STILL: '#64748b',
  ABSENT: '#334155',
}

const ACTIVITY_LABELS: Record<ActivityClass, string> = {
  TALKING: 'Falando',
  GESTURING: 'Gesticulando',
  STILL: 'Parado',
  ABSENT: 'Ausente',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function msToTime(ms: number): string {
  const s = Math.floor(ms / 1000)
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  if (h > 0) return `${h}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
  return `${m}:${String(s % 60).padStart(2, '0')}`
}

/** Returns the most recent keyframe at or before currentMs — keeps overlays static across segment */
function activeFrame(frames: FrameResult[], ms: number): FrameResult | null {
  if (!frames.length) return null
  let best = frames[0]
  for (const f of frames) {
    if (f.timestamp_ms <= ms) best = f
    else break
  }
  return best
}

// ── Skeleton SVG ──────────────────────────────────────────────────────────────

const POSE_CONNECTIONS: [number, number][] = [
  [0, 1], [1, 2], [2, 3], [3, 7],
  [0, 4], [4, 5], [5, 6], [6, 8],
  [9, 10],
  [11, 12],
  [11, 13], [13, 15],
  [12, 14], [14, 16],
  [15, 17], [15, 19], [17, 19],
  [16, 18], [16, 20], [18, 20],
  [11, 23], [12, 24], [23, 24],
]

function SkeletonOverlay({ pose, vw, vh, activity }: {
  pose: PoseData
  vw: number
  vh: number
  activity: ActivityClass
}) {
  const color = ACTIVITY_COLORS[activity]
  const lms = pose.landmarks

  const visLms = (idx: number) => lms[idx]?.visibility > 0.4 ? lms[idx] : null
  const px = (lm: PoseLandmark) => lm.x * vw
  const py = (lm: PoseLandmark) => lm.y * vh

  return (
    <g>
      {/* Connections */}
      {POSE_CONNECTIONS.map(([a, b], i) => {
        const la = visLms(a), lb = visLms(b)
        if (!la || !lb) return null
        return (
          <line key={i}
            x1={px(la)} y1={py(la)} x2={px(lb)} y2={py(lb)}
            stroke={color} strokeWidth={3} strokeOpacity={0.85}
            strokeLinecap="round"
          />
        )
      })}
      {/* Landmark dots */}
      {lms.map((lm, i) => {
        if (lm.visibility < 0.4) return null
        return (
          <circle key={i}
            cx={px(lm)} cy={py(lm)} r={5}
            fill={color} fillOpacity={0.9}
            stroke="#000" strokeWidth={1}
          />
        )
      })}
    </g>
  )
}

// ── Video player ──────────────────────────────────────────────────────────────

function VideoPlayer({
  file, frames, timeline, analyzing, progress, total,
}: {
  file: File
  frames: FrameResult[]
  timeline: TimelineSegment[]
  analyzing: boolean
  progress: number
  total: number
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [currentMs, setCurrentMs] = useState(0)
  const [durationMs, setDurationMs] = useState(0)
  const [intrinsic, setIntrinsic] = useState({ w: 1920, h: 1080 })
  const [hoveredSeg, setHoveredSeg] = useState<number | null>(null)

  const [videoUrl, setVideoUrl] = useState('')
  useEffect(() => {
    const url = URL.createObjectURL(file)
    setVideoUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const frame = useMemo(() => activeFrame(frames, currentMs), [frames, currentMs])

  const seekTo = (ms: number) => {
    if (videoRef.current) videoRef.current.currentTime = ms / 1000
  }

  const progressPct = durationMs > 0 ? (currentMs / durationMs) * 100 : 0
  const dur = durationMs || (timeline.length ? timeline[timeline.length - 1].end_ms : 0)
  const currentSeg = timeline.find(s => currentMs >= s.start_ms && currentMs < s.end_ms) ?? null
  const activity = frame?.pose?.activity_class ?? null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', background: '#0a0c12', borderRadius: 12, overflow: 'hidden', border: '1px solid #1e2235' }}>

      {/* Video + overlay */}
      <div style={{ position: 'relative', background: '#000', lineHeight: 0 }}>
        <video
          ref={videoRef}
          src={videoUrl || undefined}
          preload="auto"
          style={{
            width: '100%', maxHeight: '70vh', display: 'block',
            aspectRatio: `${intrinsic.w} / ${intrinsic.h}`,
          }}
          onTimeUpdate={e => setCurrentMs(e.currentTarget.currentTime * 1000)}
          onLoadedMetadata={e => {
            setIntrinsic({ w: e.currentTarget.videoWidth, h: e.currentTarget.videoHeight })
            setDurationMs(e.currentTarget.duration * 1000)
          }}
          controls
        />

        {/* SVG overlays: bboxes + skeleton */}
        {frame && !analyzing && (
          <svg
            style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
            viewBox={`0 0 ${intrinsic.w} ${intrinsic.h}`}
            preserveAspectRatio="xMidYMid meet"
          >
            {/* Bounding boxes */}
            {frame.elements.map((el, i) => {
              if (el.type === 'SLIDER') return null // slider bbox is full-frame, skip drawing
              const x = el.bbox.x1, y = el.bbox.y1
              const w = el.bbox.x2 - el.bbox.x1, h = el.bbox.y2 - el.bbox.y1
              const color = ELEMENT_COLORS[el.type]
              const fs = Math.round(intrinsic.h * 0.024)
              const label = `${el.type} ${Math.round(el.confidence * 100)}%`
              return (
                <g key={i}>
                  <rect x={x} y={y} width={w} height={h} fill={`${color}18`} stroke={color} strokeWidth={3} rx={6} />
                  <rect x={x} y={y} width={label.length * fs * 0.6 + 12} height={fs + 12} fill={color} rx={4} />
                  <text x={x + 6} y={y + fs + 1} fill="#fff" fontSize={fs} fontWeight="700" fontFamily="monospace">{label}</text>
                </g>
              )
            })}
            {/* Skeleton */}
            {frame.pose && frame.pose.landmarks.length > 0 && (
              <SkeletonOverlay
                pose={frame.pose}
                vw={intrinsic.w}
                vh={intrinsic.h}
                activity={frame.pose.activity_class}
              />
            )}
          </svg>
        )}

        {/* Analysis progress overlay */}
        {analyzing && (
          <div style={{
            position: 'absolute', inset: 0, background: '#00000088',
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12,
          }}>
            <div style={{ color: '#a78bfa', fontSize: 14, fontWeight: 600 }}>Dual pipeline em execução...</div>
            <div style={{ width: 240, height: 6, background: '#1e2235', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{
                height: '100%', borderRadius: 3, background: '#7c3aed',
                width: total > 0 ? `${(progress / total) * 100}%` : '0%',
                transition: 'width 0.3s',
              }} />
            </div>
            <div style={{ color: '#64748b', fontSize: 12 }}>{progress} / {total} frames</div>
          </div>
        )}

        {/* Badges top-left */}
        {!analyzing && (currentSeg || activity) && (
          <div style={{
            position: 'absolute', top: 10, left: 10,
            display: 'flex', gap: 6, pointerEvents: 'none',
          }}>
            {currentSeg && (
              <div style={{
                padding: '4px 12px', borderRadius: 20, fontSize: 12, fontWeight: 700,
                background: SCENE_COLORS[currentSeg.scene_type] + 'cc', color: '#fff',
                backdropFilter: 'blur(4px)',
              }}>
                {SCENE_LABELS[currentSeg.scene_type]}
              </div>
            )}
            {activity && activity !== 'ABSENT' && (
              <div style={{
                padding: '4px 12px', borderRadius: 20, fontSize: 12, fontWeight: 700,
                background: ACTIVITY_COLORS[activity] + 'cc', color: '#fff',
                backdropFilter: 'blur(4px)',
              }}>
                {ACTIVITY_LABELS[activity]}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Timeline scrubber */}
      <div style={{ padding: '10px 14px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div
          style={{ position: 'relative', height: 24, borderRadius: 6, overflow: 'hidden', cursor: 'pointer', display: 'flex', background: '#1e2235' }}
          onClick={e => {
            const rect = e.currentTarget.getBoundingClientRect()
            seekTo((e.clientX - rect.left) / rect.width * dur)
          }}
        >
          {timeline.map(seg => {
            const pct = dur > 0 ? ((seg.end_ms - seg.start_ms) / dur) * 100 : 0
            const color = SCENE_COLORS[seg.scene_type]
            return (
              <div key={seg.segment_id}
                onMouseEnter={() => setHoveredSeg(seg.segment_id)}
                onMouseLeave={() => setHoveredSeg(null)}
                title={`${SCENE_LABELS[seg.scene_type]}\n${msToTime(seg.start_ms)} → ${msToTime(seg.end_ms)}`}
                style={{
                  width: `${pct}%`, minWidth: 1,
                  background: hoveredSeg === seg.segment_id ? color : color + 'bb',
                  borderRight: '1px solid #0a0c12',
                  transition: 'background 0.12s',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden',
                }}
              >
                {pct > 6 && (
                  <span style={{ fontSize: 10, fontWeight: 600, color: '#fff', whiteSpace: 'nowrap', textShadow: '0 1px 2px #0008' }}>
                    {SCENE_LABELS[seg.scene_type]}
                  </span>
                )}
              </div>
            )
          })}
          <div style={{
            position: 'absolute', top: 0, left: `${progressPct}%`, width: 2, height: '100%',
            background: '#fff', boxShadow: '0 0 4px #fff8', pointerEvents: 'none', transform: 'translateX(-1px)',
          }} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#64748b', fontFamily: 'monospace' }}>
          <span style={{ color: '#94a3b8' }}>{msToTime(currentMs)}</span>
          <span>{msToTime(dur)}</span>
        </div>
      </div>
    </div>
  )
}

// ── Body analysis chart ───────────────────────────────────────────────────────

function BodyAnalysisChart({
  faceTimeline, durationMs, onSeek,
}: {
  faceTimeline: FaceTimelineEntry[]
  durationMs: number
  onSeek: (ms: number) => void
}) {
  if (!faceTimeline.length || !durationMs) return null
  const W = 600, H = 80, PAD = 4

  return (
    <div style={{ background: '#0a0c12', border: '1px solid #1e2235', borderRadius: 10, padding: '14px 16px' }}>
      <div style={{ fontSize: 11, color: '#64748b', marginBottom: 8, fontWeight: 600 }}>
        Energia corporal ao longo do vídeo — clique para navegar
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: H, cursor: 'pointer', display: 'block' }}
        onClick={e => {
          const rect = (e.currentTarget as SVGElement).getBoundingClientRect()
          const pct = (e.clientX - rect.left) / rect.width
          onSeek(pct * durationMs)
        }}
      >
        {/* Background */}
        <rect x={0} y={0} width={W} height={H} fill="#0a0c12" />
        {/* Grid lines */}
        {[0.25, 0.5, 0.75].map(v => (
          <line key={v} x1={0} y1={H - PAD - v * (H - PAD * 2)} x2={W} y2={H - PAD - v * (H - PAD * 2)}
            stroke="#1e2235" strokeWidth={1} />
        ))}
        {/* Energy bars */}
        {faceTimeline.map((pt, i) => {
          const x = (pt.timestamp_ms / durationMs) * W
          const barH = pt.energy * (H - PAD * 2)
          const color = ACTIVITY_COLORS[pt.activity_class] ?? '#64748b'
          return (
            <rect key={i}
              x={x - 1} y={H - PAD - barH} width={3} height={barH}
              fill={color} fillOpacity={0.8}
            />
          )
        })}
        {/* Activity class dots on top of bars */}
        {faceTimeline.map((pt, i) => {
          if (pt.activity_class === 'ABSENT' || pt.activity_class === 'STILL') return null
          const x = (pt.timestamp_ms / durationMs) * W
          const y = H - PAD - pt.energy * (H - PAD * 2) - 4
          const color = ACTIVITY_COLORS[pt.activity_class]
          return <circle key={i} cx={x} cy={y} r={3} fill={color} />
        })}
      </svg>
      {/* Legend */}
      <div style={{ display: 'flex', gap: 16, marginTop: 8 }}>
        {(['TALKING', 'GESTURING', 'STILL'] as ActivityClass[]).map(a => (
          <span key={a} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: '#94a3b8' }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: ACTIVITY_COLORS[a], display: 'inline-block' }} />
            {ACTIVITY_LABELS[a]}
          </span>
        ))}
      </div>
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SceneBadge({ scene }: { scene: SceneType }) {
  return (
    <span style={{
      display: 'inline-block', padding: '2px 10px', borderRadius: 20, fontSize: 11, fontWeight: 600,
      background: SCENE_COLORS[scene] + '22', color: SCENE_COLORS[scene],
      border: `1px solid ${SCENE_COLORS[scene]}55`,
    }}>
      {SCENE_LABELS[scene]}
    </span>
  )
}

function ActivityBadge({ activity }: { activity: ActivityClass }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px',
      borderRadius: 20, fontSize: 11, fontWeight: 600,
      background: ACTIVITY_COLORS[activity] + '22', color: ACTIVITY_COLORS[activity],
      border: `1px solid ${ACTIVITY_COLORS[activity]}55`,
    }}>
      {ACTIVITY_LABELS[activity]}
    </span>
  )
}

function ElementTag({ type, confidence }: { type: ElementType; confidence: number }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 10px',
      borderRadius: 6, fontSize: 11, fontWeight: 600,
      background: ELEMENT_COLORS[type] + '18', color: ELEMENT_COLORS[type],
      border: `1px solid ${ELEMENT_COLORS[type]}40`,
    }}>
      {type}
      <span style={{ opacity: 0.7, fontWeight: 400 }}>{Math.round(confidence * 100)}%</span>
    </span>
  )
}

function FrameCard({ frame }: { frame: FrameResult }) {
  const [expanded, setExpanded] = useState(false)
  const pose = frame.pose
  return (
    <div style={{ background: '#13161f', border: '1px solid #1e2235', borderRadius: 10, overflow: 'hidden' }}>
      <button
        onClick={() => setExpanded(v => !v)}
        style={{
          width: '100%', background: 'transparent', border: 'none', cursor: 'pointer',
          padding: '10px 16px', display: 'flex', alignItems: 'center', gap: 10, textAlign: 'left',
        }}
      >
        <span style={{ color: '#64748b', fontSize: 11, fontFamily: 'monospace', minWidth: 80, flexShrink: 0 }}>
          #{frame.frame_id} · {msToTime(frame.timestamp_ms)}
        </span>
        <SceneBadge scene={frame.scene_type} />
        {pose && <ActivityBadge activity={pose.activity_class} />}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', flex: 1 }}>
          {frame.elements.map((el, i) => <ElementTag key={i} type={el.type} confidence={el.confidence} />)}
        </div>
        <span style={{ color: '#64748b', fontSize: 12, flexShrink: 0 }}>{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div style={{ borderTop: '1px solid #1e2235', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Elements detail */}
          {frame.elements.map((el, i) => (
            <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
              <ElementTag type={el.type} confidence={el.confidence} />
              <div style={{ fontFamily: 'monospace', fontSize: 11, color: '#94a3b8' }}>
                <span style={{ color: '#64748b' }}>bbox: </span>
                [{el.bbox.x1}, {el.bbox.y1}, {el.bbox.x2}, {el.bbox.y2}]
              </div>
              {Object.entries(el.metadata).map(([k, v]) => (
                <div key={k} style={{ fontFamily: 'monospace', fontSize: 11, color: '#94a3b8' }}>
                  <span style={{ color: '#64748b' }}>{k}: </span>{String(v)}
                </div>
              ))}
            </div>
          ))}

          {/* Pose / body analysis */}
          {pose && (
            <div style={{ borderTop: '1px solid #1e223580', paddingTop: 10 }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: '#475569', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>
                Análise Corporal
              </div>
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 8 }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, alignItems: 'flex-start' }}>
                  <span style={{ fontSize: 10, color: '#64748b' }}>Atividade</span>
                  <ActivityBadge activity={pose.activity_class} />
                </div>
                {[
                  { label: 'Energia', value: pose.energy },
                  { label: 'Vel. Pulso', value: pose.wrist_velocity },
                ].map(({ label, value }) => (
                  <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <span style={{ fontSize: 10, color: '#64748b' }}>{label}</span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <div style={{ width: 80, height: 5, background: '#1e2235', borderRadius: 3, overflow: 'hidden' }}>
                        <div style={{ height: '100%', width: `${value * 100}%`, background: ACTIVITY_COLORS[pose.activity_class], borderRadius: 3 }} />
                      </div>
                      <span style={{ fontSize: 11, color: '#94a3b8', fontFamily: 'monospace' }}>{(value * 100).toFixed(0)}%</span>
                    </div>
                  </div>
                ))}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <span style={{ fontSize: 10, color: '#64748b' }}>Landmarks</span>
                  <span style={{ fontSize: 11, color: '#94a3b8', fontFamily: 'monospace' }}>
                    {pose.landmarks.filter(l => l.visibility > 0.4).length}/{pose.landmarks.length} visíveis
                  </span>
                </div>
              </div>
            </div>
          )}

          {/* Raw detector output */}
          {frame.raw && (
            <div style={{ borderTop: '1px solid #1e223580', paddingTop: 8 }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: '#475569', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>
                Raw detector output
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '2px 16px', fontFamily: 'monospace', fontSize: 11, color: '#94a3b8' }}>
                {Object.entries(frame.raw).map(([k, v]) => (
                  <span key={k}><span style={{ color: '#64748b' }}>{k}: </span>{String(v)}</span>
                ))}
              </div>
            </div>
          )}

          {/* OCR Slide Text */}
          {frame.slide_text && (
            <div style={{ borderTop: '1px solid #1e223580', paddingTop: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span style={{ fontSize: 10, fontWeight: 600, color: '#a855f7', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  📝 Texto do Slide
                </span>
                {frame.slide_text_id && (
                  <span style={{ fontSize: 10, color: '#475569' }}>Slide #{frame.slide_text_id}</span>
                )}
              </div>
              <pre style={{
                margin: 0, padding: '8px 10px', background: '#0a0c12', border: '1px solid #1e223580',
                borderRadius: 6, borderLeft: '3px solid #a855f7',
                fontSize: 11, color: '#94a3b8', lineHeight: 1.5,
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'system-ui, sans-serif',
              }}>{frame.slide_text}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function PipelineOverview() {
  const steps = [
    { label: 'Pipeline A', desc: 'Hash + Slide', icon: '📊' },
    { label: 'Pipeline B', desc: 'Face + Pose', icon: '🦴' },
    { label: 'Merge', desc: 'Keyframes', icon: '🔀' },
    { label: 'Scene Classifier', desc: 'Combinado', icon: '🎬' },
    { label: 'Timeline', desc: 'Segmentos', icon: '📈' },
  ]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
      {steps.map((step, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
            padding: '10px 14px', background: '#13161f', border: '1px solid #1e2235',
            borderRadius: 8, minWidth: 90,
          }}>
            <span style={{ fontSize: 20 }}>{step.icon}</span>
            <span style={{ fontSize: 12, fontWeight: 600, color: '#e2e8f0', textAlign: 'center' }}>{step.label}</span>
            <span style={{ fontSize: 10, color: '#64748b', textAlign: 'center' }}>{step.desc}</span>
          </div>
          {i < steps.length - 1 && <span style={{ color: '#334155', fontSize: 18 }}>→</span>}
        </div>
      ))}
    </div>
  )
}

function UploadZone({ onFileSelect }: { onFileSelect: (file: File) => void }) {
  const [dragging, setDragging] = useState(false)
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file?.type.startsWith('video/')) onFileSelect(file)
  }, [onFileSelect])
  return (
    <label
      onDragOver={e => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        gap: 12, padding: '48px 24px',
        border: `2px dashed ${dragging ? '#7c3aed' : '#1e2235'}`,
        borderRadius: 12, cursor: 'pointer',
        background: dragging ? '#7c3aed11' : '#0a0c12', transition: 'all 0.2s',
      }}
    >
      <input type="file" accept="video/*" style={{ display: 'none' }}
        onChange={e => { const f = e.target.files?.[0]; if (f) onFileSelect(f) }} />
      <div style={{ fontSize: 40 }}>🎬</div>
      <div style={{ color: '#94a3b8', fontSize: 15, fontWeight: 500 }}>Arraste um vídeo ou clique para selecionar</div>
      <div style={{ color: '#475569', fontSize: 12 }}>.mp4 · .webm · .mov · .mkv</div>
    </label>
  )
}

// ── AI Keyframes Section ──────────────────────────────────────────────────────

function AIKeyframesSection({ keyframes, onUpdate }: {
  keyframes: AIKeyframe[]
  onUpdate: (kfs: AIKeyframe[]) => void
}) {
  const [interpreting, setInterpreting] = useState<number | null>(null)
  const [expandedKf, setExpandedKf] = useState<number | null>(null)
  const [interpretAllRunning, setInterpretAllRunning] = useState(false)
  const [zoomedFrameIdx, setZoomedFrameIdx] = useState<number | null>(null)

  // Keyboard navigation for zoom modal
  useEffect(() => {
    if (zoomedFrameIdx === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight') setZoomedFrameIdx(i => i !== null ? Math.min(i + 1, keyframes.length - 1) : null)
      else if (e.key === 'ArrowLeft') setZoomedFrameIdx(i => i !== null ? Math.max(i - 1, 0) : null)
      else if (e.key === 'Escape') setZoomedFrameIdx(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoomedFrameIdx, keyframes.length])

  const interpretOne = useCallback(async (kfIndex: number) => {
    setInterpreting(kfIndex)
    try {
      const res = await fetch(`${NEO_VISION_API}/api/interpret-keyframe/${kfIndex}`, { method: 'POST' })
      const data = await res.json()
      if (data.slide_text_md) {
        const updated = [...keyframes]
        updated[kfIndex] = { ...updated[kfIndex], interpreted: true, slide_text_md: data.slide_text_md }
        onUpdate(updated)
      }
    } catch (e) {
      console.error('Interpret error:', e)
    } finally {
      setInterpreting(null)
    }
  }, [keyframes, onUpdate])

  const interpretAll = useCallback(async () => {
    setInterpretAllRunning(true)
    for (let i = 0; i < keyframes.length; i++) {
      if (!keyframes[i].interpreted) {
        setInterpreting(i)
        try {
          const res = await fetch(`${NEO_VISION_API}/api/interpret-keyframe/${i}`, { method: 'POST' })
          const data = await res.json()
          if (data.slide_text_md) {
            keyframes[i] = { ...keyframes[i], interpreted: true, slide_text_md: data.slide_text_md }
            onUpdate([...keyframes])
          }
        } catch (e) {
          console.error(`Interpret error at ${i}:`, e)
        }
      }
    }
    setInterpreting(null)
    setInterpretAllRunning(false)
  }, [keyframes, onUpdate])

  const interpretedCount = keyframes.filter(k => k.interpreted).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
          ✨ AI Keyframes ({keyframes.length})
        </h3>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: '#475569' }}>
            {interpretedCount}/{keyframes.length} interpretados
          </span>
          {interpretedCount < keyframes.length && (
            <button
              onClick={interpretAll}
              disabled={interpretAllRunning}
              style={{
                padding: '5px 14px', fontSize: 11, fontWeight: 600,
                background: interpretAllRunning ? '#374151' : 'linear-gradient(135deg, #7c3aed, #448aff)',
                color: '#fff', border: 'none', borderRadius: 6,
                cursor: interpretAllRunning ? 'wait' : 'pointer',
              }}
            >
              {interpretAllRunning ? '⏳ Interpretando...' : '🚀 Interpretar Todos'}
            </button>
          )}
        </div>
      </div>

      {/* Info banner */}
      <div style={{
        padding: '10px 14px', fontSize: 11, color: '#94a3b8', lineHeight: 1.6,
        background: 'linear-gradient(90deg, rgba(124,58,237,0.08), transparent)',
        border: '1px solid #7c3aed33', borderRadius: 8,
      }}>
        Keyframes selecionados automaticamente por <strong style={{ color: '#a78bfa' }}>diferença de texto OCR ≥35%</strong>.
        São os momentos ideais para enviar ao GPT-4o-mini para interpretação estruturada.
      </div>

      {/* Keyframe cards */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {keyframes.map((kf, i) => {
          const isExpanded = expandedKf === i
          const isRunning = interpreting === i
          return (
            <div key={i} style={{
              background: '#13161f',
              border: kf.interpreted ? '1px solid #448aff44' : '1px solid #1e2235',
              borderRadius: 12,
              overflow: 'hidden',
              transition: 'border-color 0.2s',
            }}>
              {/* Card header row */}
              <div
                style={{
                  display: 'flex', alignItems: 'center', gap: 12,
                  padding: '10px 14px', cursor: 'pointer',
                  background: kf.interpreted
                    ? 'linear-gradient(90deg, rgba(68,138,255,0.06), transparent)'
                    : 'transparent',
                }}
                onClick={() => setExpandedKf(isExpanded ? null : i)}
              >
                {/* Thumbnail with maximize button */}
                <div
                  style={{ position: 'relative', flexShrink: 0, width: 100, height: 56 }}
                  onClick={e => e.stopPropagation()}
                >
                  <img
                    src={`${NEO_VISION_API}/api/frame-thumbnail/${kf.frame_idx}?width=160`}
                    alt={`Frame ${kf.frame_idx}`}
                    style={{
                      width: 100, height: 56, objectFit: 'cover',
                      borderRadius: 6, display: 'block',
                      border: kf.interpreted ? '2px solid #448aff55' : '2px solid #1e2235',
                    }}
                  />
                  <button
                    title="Ver frame original"
                    onClick={() => setZoomedFrameIdx(i)}
                    style={{
                      position: 'absolute', top: 4, right: 4,
                      width: 20, height: 20, padding: 0,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      background: 'rgba(0,0,0,0.65)', color: '#e2e8f0',
                      border: 'none', borderRadius: 4, cursor: 'pointer',
                      fontSize: 11, lineHeight: 1,
                      backdropFilter: 'blur(2px)',
                    }}
                  >
                    ⛶
                  </button>
                </div>

                {/* Info */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span style={{
                      fontSize: 12, fontWeight: 700,
                      color: kf.interpreted ? '#448aff' : '#e2e8f0',
                    }}>
                      {kf.interpreted ? '✨' : '📊'} Slide #{kf.slide_id}
                    </span>
                    <span style={{
                      fontFamily: 'monospace', fontSize: 10, color: '#64748b',
                    }}>
                      {msToTime(kf.timestamp_ms)}
                    </span>
                    <span style={{
                      padding: '1px 7px', borderRadius: 10, fontSize: 10, fontWeight: 600,
                      background: `hsl(${kf.text_diff_ratio * 120}, 80%, 20%)`,
                      color: `hsl(${kf.text_diff_ratio * 120}, 80%, 70%)`,
                      border: `1px solid hsl(${kf.text_diff_ratio * 120}, 80%, 30%)`,
                    }}>
                      Δ {(kf.text_diff_ratio * 100).toFixed(0)}%
                    </span>
                    {kf.interpreted && (
                      <span style={{
                        padding: '1px 7px', borderRadius: 10, fontSize: 10, fontWeight: 600,
                        background: '#22c55e1a', color: '#22c55e', border: '1px solid #22c55e33',
                      }}>
                        ✓ IA
                      </span>
                    )}
                  </div>
                  <div style={{
                    fontSize: 11, color: '#64748b', marginTop: 3,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {kf.ocr_text.slice(0, 80)}{kf.ocr_text.length > 80 ? '...' : ''}
                  </div>
                </div>

                {/* Actions */}
                <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                  {!kf.interpreted && (
                    <button
                      onClick={(e) => { e.stopPropagation(); interpretOne(i) }}
                      disabled={isRunning}
                      style={{
                        padding: '5px 12px', fontSize: 11, fontWeight: 600,
                        background: isRunning ? '#374151' : 'rgba(124,58,237,0.2)',
                        color: isRunning ? '#94a3b8' : '#a78bfa',
                        border: '1px solid #7c3aed44', borderRadius: 6,
                        cursor: isRunning ? 'wait' : 'pointer',
                      }}
                    >
                      {isRunning ? '⏳' : '✨ Interpretar'}
                    </button>
                  )}
                  <span style={{ color: '#475569', fontSize: 14, transform: `rotate(${isExpanded ? 180 : 0}deg)`, transition: 'transform 0.2s' }}>
                    ▾
                  </span>
                </div>
              </div>

              {/* Expanded content */}
              {isExpanded && (
                <div style={{ borderTop: '1px solid #1e2235', padding: '14px 16px' }}>
                  {kf.slide_text_md ? (
                    <div>
                      <div style={{ fontSize: 11, color: '#64748b', marginBottom: 8, fontWeight: 600 }}>
                        ✨ Interpretação IA (GPT-4o-mini)
                      </div>
                      <div style={{
                        padding: '12px 16px', background: '#0a0c14',
                        border: '1px solid #448aff33', borderRadius: 8,
                        fontSize: 12, color: '#cbd5e1', lineHeight: 1.8,
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                      }}>
                        {kf.slide_text_md}
                      </div>
                    </div>
                  ) : (
                    <div>
                      <div style={{ fontSize: 11, color: '#64748b', marginBottom: 8, fontWeight: 600 }}>
                        📊 OCR (Tesseract)
                      </div>
                    </div>
                  )}
                  <pre style={{
                    margin: kf.slide_text_md ? '12px 0 0' : 0,
                    padding: '10px 14px', background: '#080a10',
                    border: '1px solid #1e2235', borderRadius: 6,
                    fontSize: 11, color: kf.slide_text_md ? '#475569' : '#94a3b8',
                    lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                    fontFamily: 'monospace', maxHeight: 200, overflowY: 'auto',
                  }}>
                    {kf.ocr_text}
                  </pre>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Frame zoom modal */}
      {zoomedFrameIdx !== null && (
        <div
          onClick={() => setZoomedFrameIdx(null)}
          style={{
            position: 'fixed', inset: 0, zIndex: 2000,
            background: 'rgba(0,0,0,0.92)',
            backdropFilter: 'blur(8px)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 24,
          }}
        >
          {/* Prev arrow */}
          <button
            onClick={e => { e.stopPropagation(); setZoomedFrameIdx(i => i !== null ? Math.max(i - 1, 0) : null) }}
            disabled={zoomedFrameIdx === 0}
            style={{
              position: 'fixed', left: 20, top: '50%', transform: 'translateY(-50%)',
              width: 44, height: 44, padding: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: zoomedFrameIdx === 0 ? 'rgba(30,34,53,0.4)' : 'rgba(30,34,53,0.85)',
              color: zoomedFrameIdx === 0 ? '#374151' : '#e2e8f0',
              border: '1px solid #2d3748', borderRadius: '50%',
              cursor: zoomedFrameIdx === 0 ? 'default' : 'pointer',
              fontSize: 20, backdropFilter: 'blur(4px)',
              boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
              transition: 'all 0.15s',
            }}
          >‹</button>

          <div
            onClick={e => e.stopPropagation()}
            style={{ position: 'relative', maxWidth: '90vw', maxHeight: '90vh' }}
          >
            <img
              src={`${NEO_VISION_API}/api/frame-thumbnail/${keyframes[zoomedFrameIdx!].frame_idx}?width=1280`}
              alt="Frame ampliado"
              style={{
                maxWidth: '90vw', maxHeight: '85vh',
                objectFit: 'contain', display: 'block',
                borderRadius: 12,
                border: '1px solid #448aff55',
                boxShadow: '0 32px 80px rgba(0,0,0,0.8)',
              }}
            />
            {/* Close button */}
            <button
              onClick={() => setZoomedFrameIdx(null)}
              style={{
                position: 'absolute', top: -14, right: -14,
                width: 32, height: 32, padding: 0,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: '#1e2235', color: '#e2e8f0',
                border: '1px solid #2d3748', borderRadius: '50%',
                cursor: 'pointer', fontSize: 16, fontWeight: 700,
                boxShadow: '0 4px 12px rgba(0,0,0,0.5)',
              }}
            >
              ✕
            </button>
            {/* Frame label */}
            <div style={{
              position: 'absolute', bottom: -36, left: 0, right: 0,
              textAlign: 'center', fontSize: 12, color: '#64748b',
            }}>
              Keyframe {zoomedFrameIdx! + 1} de {keyframes.length}
              &nbsp;·&nbsp;Frame #{keyframes[zoomedFrameIdx!].frame_idx}
              &nbsp;·&nbsp;← → para navegar · Esc para fechar
            </div>
          </div>

          {/* Next arrow */}
          <button
            onClick={e => { e.stopPropagation(); setZoomedFrameIdx(i => i !== null ? Math.min(i + 1, keyframes.length - 1) : null) }}
            disabled={zoomedFrameIdx === keyframes.length - 1}
            style={{
              position: 'fixed', right: 20, top: '50%', transform: 'translateY(-50%)',
              width: 44, height: 44, padding: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: zoomedFrameIdx === keyframes.length - 1 ? 'rgba(30,34,53,0.4)' : 'rgba(30,34,53,0.85)',
              color: zoomedFrameIdx === keyframes.length - 1 ? '#374151' : '#e2e8f0',
              border: '1px solid #2d3748', borderRadius: '50%',
              cursor: zoomedFrameIdx === keyframes.length - 1 ? 'default' : 'pointer',
              fontSize: 20, backdropFilter: 'blur(4px)',
              boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
              transition: 'all 0.15s',
            }}
          >›</button>
        </div>
      )}
    </div>
  )
}

// ── Section wrapper (outside NeoVisionPanel to avoid remounts) ────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <h3 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {title}
      </h3>
      {children}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function NeoVisionPanel() {
  const [file, setFile] = useState<File | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [progress, setProgress] = useState(0)
  const [total, setTotal] = useState(0)
  const [errorMsg, setErrorMsg] = useState('')
  const [frames, setFrames] = useState<FrameResult[]>([])
  const [timeline, setTimeline] = useState<TimelineSegment[]>([])
  const [videoInfo, setVideoInfo] = useState<VideoInfo | null>(null)
  const [faceTimeline, setFaceTimeline] = useState<FaceTimelineEntry[]>([])
  const [slideTexts, setSlideTexts] = useState<SlideTextEntry[]>([])
  const [aiKeyframes, setAiKeyframes] = useState<AIKeyframe[]>([])
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const videoPlayerRef = useRef<HTMLVideoElement | null>(null)

  const stopPoll = () => { if (pollRef.current) clearTimeout(pollRef.current) }

  const pollProgress = useCallback(async () => {
    try {
      const res = await fetch(`${NEO_VISION_API}/api/analyze/progress`)
      const data = await res.json()
      setProgress(data.progress)
      setTotal(data.total)
      if (data.error) {
        setErrorMsg(data.error)
        setPhase('error')
      } else if (data.done) {
        const r = await fetch(`${NEO_VISION_API}/api/analyze/results`)
        const results = await r.json()
        setFrames(results.frames ?? [])
        setTimeline(results.segments ?? [])
        setVideoInfo(results.video_info ?? null)
        setFaceTimeline(results.face_timeline ?? [])
        setSlideTexts(results.slide_texts ?? [])
        setAiKeyframes(results.ai_keyframes ?? [])
        setPhase('done')
      } else {
        pollRef.current = setTimeout(pollProgress, 500)
      }
    } catch {
      pollRef.current = setTimeout(pollProgress, 1000)
    }
  }, [])

  const onFileSelect = useCallback(async (f: File) => {
    stopPoll()
    setFile(f)
    setPhase('uploading')
    setFrames([])
    setTimeline([])
    setVideoInfo(null)
    setFaceTimeline([])
    setSlideTexts([])
    setAiKeyframes([])
    setErrorMsg('')

    try {
      const fd = new FormData()
      fd.append('file', f)
      const res = await fetch(`${NEO_VISION_API}/api/upload`, { method: 'POST', body: fd })
      const data = await res.json()
      if (data.error) { setErrorMsg(data.error); setPhase('error'); return }

      setPhase('analyzing')
      await fetch(`${NEO_VISION_API}/api/analyze?fps_sample=1`, { method: 'POST' })
      pollProgress()
    } catch {
      setErrorMsg(`Backend offline (${NEO_VISION_API}). Verifique se o neo-vision está rodando.`)
      setPhase('error')
    }
  }, [pollProgress])

  useEffect(() => () => stopPoll(), [])

  const analyzing = phase === 'uploading' || phase === 'analyzing'

  const handleChartSeek = useCallback((ms: number) => {
    if (videoPlayerRef.current) videoPlayerRef.current.currentTime = ms / 1000
  }, [])

  return (
    <div style={{
      flex: 1, padding: '24px', display: 'flex', flexDirection: 'column', gap: 28,
      overflowY: 'auto', color: '#e2e8f0', fontFamily: 'system-ui, sans-serif',
    }}>

      {/* Upload / file bar */}
      <Section title="Entrada de Vídeo">
        {file ? (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px',
            background: '#13161f', border: '1px solid #1e2235', borderRadius: 10,
          }}>
            <span style={{ fontSize: 22 }}>🎬</span>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 500 }}>{file.name}</div>
              <div style={{ fontSize: 12, color: '#64748b' }}>
                {(file.size / 1024 / 1024).toFixed(1)} MB
                {videoInfo && ` · ${videoInfo.width}×${videoInfo.height} · ${videoInfo.fps.toFixed(1)}fps · ${msToTime(videoInfo.duration_ms)}`}
              </div>
            </div>
            {phase === 'uploading' && <span style={{ fontSize: 12, color: '#f97316', fontWeight: 600 }}>Enviando...</span>}
            {phase === 'analyzing' && (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
                <span style={{ fontSize: 12, color: '#a78bfa', fontWeight: 600 }}>Analisando {progress}/{total}</span>
                <div style={{ width: 120, height: 4, background: '#1e2235', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{ height: '100%', background: '#7c3aed', borderRadius: 2, width: total > 0 ? `${(progress / total) * 100}%` : '0%', transition: 'width 0.3s' }} />
                </div>
              </div>
            )}
            {phase === 'done' && (
              <span style={{ padding: '4px 12px', borderRadius: 20, fontSize: 12, fontWeight: 600, background: '#22c55e22', color: '#22c55e', border: '1px solid #22c55e44' }}>
                {videoInfo?.frames_analyzed} keyframes
              </span>
            )}
            {phase === 'error' && <span style={{ fontSize: 12, color: '#ef4444', fontWeight: 600 }}>Erro</span>}
            <button onClick={() => { stopPoll(); setFile(null); setPhase('idle') }}
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: '#64748b', fontSize: 18 }}>✕</button>
          </div>
        ) : (
          <UploadZone onFileSelect={onFileSelect} />
        )}
        {phase === 'error' && (
          <div style={{ padding: '12px 16px', background: '#ef444418', border: '1px solid #ef444444', borderRadius: 8, fontSize: 13, color: '#ef4444' }}>
            {errorMsg}
          </div>
        )}
      </Section>

      {/* Video player */}
      {file && phase !== 'idle' && (
        <Section title="Reprodutor com Overlays">
          {phase === 'done' && (
            <div style={{ fontSize: 12, color: '#475569', marginTop: -6 }}>
              Keyframe detection 1fps · skeleton corporal · overlays sincronizados por segmento
            </div>
          )}
          <VideoPlayer
            file={file}
            frames={frames}
            timeline={timeline}
            analyzing={analyzing}
            progress={progress}
            total={total}
          />
        </Section>
      )}

      {/* Body analysis chart */}
      {phase === 'done' && faceTimeline.length > 0 && (
        <Section title="Análise Corporal">
          <BodyAnalysisChart
            faceTimeline={faceTimeline}
            durationMs={videoInfo?.duration_ms ?? 0}
            onSeek={handleChartSeek}
          />
        </Section>
      )}

      {/* Pipeline */}
      <Section title="Pipeline de Análise">
        <PipelineOverview />
      </Section>

      {/* Results */}
      {phase === 'done' && frames.length > 0 && (
        <>
          {timeline.length > 0 && (
            <Section title="Segmentos Detectados">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {timeline.map(seg => (
                  <div key={seg.segment_id} style={{
                    display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 12px',
                    background: '#13161f', border: '1px solid #1e2235', borderRadius: 8,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                      <div style={{ width: 10, height: 10, borderRadius: 2, background: SCENE_COLORS[seg.scene_type], flexShrink: 0 }} />
                      <span style={{ fontFamily: 'monospace', fontSize: 11, color: '#64748b', minWidth: 130 }}>
                        {msToTime(seg.start_ms)} → {msToTime(seg.end_ms)}
                      </span>
                      <SceneBadge scene={seg.scene_type} />
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        {seg.dominant_elements.map(el => (
                          <span key={el} style={{
                            fontSize: 11, fontWeight: 600,
                            color: ELEMENT_COLORS[el as ElementType] ?? '#94a3b8',
                            background: (ELEMENT_COLORS[el as ElementType] ?? '#94a3b8') + '18',
                            border: `1px solid ${(ELEMENT_COLORS[el as ElementType] ?? '#94a3b8')}40`,
                            padding: '1px 8px', borderRadius: 4,
                          }}>{el}</span>
                        ))}
                      </div>
                    </div>
                    {seg.slide_texts && seg.slide_texts.length > 0 && (
                      <div style={{ marginLeft: 22, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        {seg.slide_texts.map(st => (
                          <div key={st.slide_id} style={{
                            padding: '6px 10px', background: '#0a0c12', border: '1px solid #1e223580',
                            borderRadius: 6, borderLeft: '3px solid #a855f7',
                          }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                              <span style={{ fontSize: 10, fontWeight: 700, color: '#a855f7' }}>📝 Slide #{st.slide_id}</span>
                              <span style={{ fontSize: 10, color: '#475569' }}>
                                {st.frame_count} frame{st.frame_count > 1 ? 's' : ''}
                              </span>
                            </div>
                            <pre style={{
                              margin: 0, fontSize: 11, color: '#94a3b8', lineHeight: 1.5,
                              whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'system-ui, sans-serif',
                            }}>{st.text}</pre>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Section>
          )}

          {slideTexts.length > 0 && (
            <Section title={`📝 Textos dos Slides (${slideTexts.length})`}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {slideTexts.map(st => (
                  <div key={st.slide_id} style={{
                    padding: '12px 16px', background: '#13161f', border: '1px solid #1e2235',
                    borderRadius: 10, borderLeft: '4px solid #a855f7',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                      <span style={{
                        padding: '2px 10px', borderRadius: 20, fontSize: 12, fontWeight: 700,
                        background: '#a855f722', color: '#a855f7', border: '1px solid #a855f744',
                      }}>
                        Slide #{st.slide_id}
                      </span>
                      <span style={{ fontSize: 11, color: '#475569' }}>
                        {st.frame_count} frame{st.frame_count > 1 ? 's' : ''}
                        {' · '}
                        {msToTime(st.first_timestamp_ms)} → {msToTime(st.last_timestamp_ms)}
                      </span>
                    </div>
                    <pre style={{
                      margin: 0, padding: '10px 12px', background: '#0a0c12', border: '1px solid #1e223580',
                      borderRadius: 8, fontSize: 13, color: '#e2e8f0', lineHeight: 1.6,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'system-ui, sans-serif',
                    }}>{st.text}</pre>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {/* AI Keyframes */}
          {aiKeyframes.length > 0 && (
            <AIKeyframesSection
              keyframes={aiKeyframes}
              onUpdate={setAiKeyframes}
            />
          )}

          <Section title={`Keyframes Analisados (${frames.length})`}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {frames.map(frame => <FrameCard key={frame.frame_id} frame={frame} />)}
            </div>
          </Section>

          <Section title="Exemplo de Frame (JSON)">
            <div style={{
              padding: '16px', background: '#0a0c12', border: '1px solid #1e2235',
              borderRadius: 10, fontFamily: 'monospace', fontSize: 12, color: '#94a3b8',
              overflowX: 'auto', lineHeight: 1.6,
            }}>
              <pre style={{ margin: 0 }}>{JSON.stringify(frames[1] ?? frames[0], null, 2)}</pre>
            </div>
          </Section>
        </>
      )}
    </div>
  )
}
