import { useEffect, useState, useCallback } from 'react';

const API = '/api/v1/knowledge-graph';

// ── Cores por tipo ─────────────────────────────────────────────────────────────
const TYPE_COLORS: Record<string, string> = {
  person:           '#818cf8',
  project:          '#60a5fa',
  company:          '#34d399',
  device:           '#f472b6',
  technology:       '#22d3ee',
  document:         '#fbbf24',
  workout:          '#a78bfa',
  exercise:         '#c084fc',
  muscle_group:     '#f87171',
  meal:             '#f472b6',
  food:             '#a3e635',
  supplement:       '#22d3ee',
  health_state:     '#34d399',
  restriction:      '#f87171',
  routine:          '#fbbf24',
  goal:             '#fb923c',
  preference:       '#2dd4bf',
  vehicle:          '#60a5fa',
  property:         '#fbbf24',
  account:          '#60a5fa',
  pet:              '#f472b6',
  place:            '#60a5fa',
  software:         '#22d3ee',
  hardware_problem: '#f87171',
};
const DEFAULT_COLOR = '#94a3b8';

function typeColor(type: string) {
  return TYPE_COLORS[type] ?? DEFAULT_COLOR;
}

// ── Utilitários ────────────────────────────────────────────────────────────────
function fmtDate(iso: string | null | undefined) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

function fmtDateTime(iso: string | null | undefined) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function sensitivityBadge(s: string) {
  const map: Record<string, { bg: string; color: string }> = {
    public:    { bg: '#065f46', color: '#34d399' },
    internal:  { bg: '#1e3a5f', color: '#60a5fa' },
    private:   { bg: '#312e81', color: '#a78bfa' },
    sensitive: { bg: '#9f1239', color: '#f87171' },
    secret:    { bg: '#7f1d1d', color: '#fca5a5' },
  };
  const sty = map[s] ?? { bg: '#1e293b', color: '#94a3b8' };
  return { ...sty, label: s };
}

// ── Tipos ─────────────────────────────────────────────────────────────────────
interface Entity {
  id: string;
  workspace_id: string;
  space_id: string | null;
  collection_id: string | null;
  name: string;
  type: string;
  aliases: string[];
  metadata: Record<string, unknown>;
  external_refs: Record<string, unknown>;
  confidence: number;
  sensitivity: string;
  source: string | null;
  source_message_id: string | null;
  last_used_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface Field {
  id: string;
  entity_id: string;
  key: string;
  value_type: string;
  value: unknown;
  unit: string | null;
  confidence: number;
  sensitivity: string;
  source: string | null;
  source_message_id: string | null;
  actor: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

interface Relation {
  id: string;
  workspace_id: string;
  from_entity_id: string;
  relation_type: string;
  to_entity_id: string;
  metadata: Record<string, unknown>;
  confidence: number;
  sensitivity: string;
  valid_from: string | null;
  valid_until: string | null;
  created_at: string | null;
  updated_at: string | null;
  from_name: string | null;
  from_type: string | null;
  to_name: string | null;
  to_type: string | null;
}

interface Workspace {
  id: string;
  owner_user_id: string | null;
  name: string;
  type: string;
  created_at: string | null;
  updated_at: string | null;
}

interface Collection {
  id: string;
  workspace_id: string;
  space_id: string | null;
  name: string;
  type: string;
  schema: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

interface AuditEntry {
  id: string;
  workspace_id: string | null;
  actor_type: string | null;
  actor_id: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  before: unknown | null;
  after: unknown | null;
  reason: string | null;
  confidence: number | null;
  source_message_id: string | null;
  request_id: string | null;
  created_at: string | null;
}

// ── Componente principal ───────────────────────────────────────────────────────
export default function PKSPanel() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWs, setSelectedWs] = useState<string>('');

  const [entities, setEntities] = useState<Entity[]>([]);
  const [loadingEntities, setLoadingEntities] = useState(false);

  const [selectedEntity, setSelectedEntity] = useState<Entity | null>(null);
  const [entityFields, setEntityFields] = useState<Field[]>([]);
  const [entityRelations, setEntityRelations] = useState<Relation[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const [rawData, setRawData] = useState<Record<string, unknown> | null>(null);
  const [loadingRaw, setLoadingRaw] = useState(false);

  const [auditLog, setAuditLog] = useState<AuditEntry[]>([]);
  const [loadingAudit, setLoadingAudit] = useState(false);

  const [activeTab, setActiveTab] = useState<'entities' | 'audit' | 'collections'>('entities');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');

  const [collections, setCollections] = useState<Collection[]>([]);
  const [loadingCollections, setLoadingCollections] = useState(false);
  const [collectionsMap, setCollectionsMap] = useState<Record<string, string>>({});

  // ── Fetch workspaces ───────────────────────────────────────────────────────
  const fetchWorkspaces = useCallback(async () => {
    try {
      const res = await fetch(`${API}/workspaces`);
      if (res.ok) {
        const data = await res.json();
        setWorkspaces(data.workspaces ?? []);
        if (!selectedWs && data.workspaces?.length > 0) {
          setSelectedWs(data.workspaces[0].id);
        }
      }
    } catch { /* ignore */ }
  }, [selectedWs]);

  // ── Fetch entities ────────────────────────────────────────────────────────
  const fetchEntities = useCallback(async (wsId: string, type?: string) => {
    setLoadingEntities(true);
    try {
      const params = new URLSearchParams({ workspace_id: wsId });
      if (type) params.set('entity_type', type);
      const res = await fetch(`${API}/entities?${params}`);
      if (res.ok) {
        const data = await res.json();
        setEntities(data.entities ?? []);
      }
    } catch { /* ignore */ } finally {
      setLoadingEntities(false);
    }
  }, []);

  // ── Fetch entity detail ──────────────────────────────────────────────────
  const fetchEntityDetail = useCallback(async (entityId: string) => {
    setLoadingDetail(true);
    setEntityFields([]);
    setEntityRelations([]);
    setShowRaw(false);
    setRawData(null);
    try {
      const res = await fetch(`${API}/entities/${entityId}`);
      if (res.ok) {
        const data = await res.json();
        setSelectedEntity(data.entity);
        setEntityFields(data.fields ?? []);
        setEntityRelations(data.relations ?? []);
      }
    } catch { /* ignore */ } finally {
      setLoadingDetail(false);
    }
  }, []);

  // ── Fetch raw entity row ────────────────────────────────────────────────
  const fetchRawEntity = useCallback(async (entityId: string) => {
    setLoadingRaw(true);
    try {
      const res = await fetch(`${API}/entities/${entityId}/raw`);
      if (res.ok) {
        const data = await res.json();
        setRawData(data.row);
        setShowRaw(true);
      }
    } catch { /* ignore */ } finally {
      setLoadingRaw(false);
    }
  }, []);

  // ── Fetch audit log ──────────────────────────────────────────────────────
  const fetchAudit = useCallback(async (wsId: string) => {
    setLoadingAudit(true);
    try {
      const res = await fetch(`${API}/audit?workspace_id=${wsId}`);
      if (res.ok) {
        const data = await res.json();
        setAuditLog(data.entries ?? []);
      }
    } catch { /* ignore */ } finally {
      setLoadingAudit(false);
    }
  }, []);

  // ── Fetch collections ───────────────────────────────────────────────────────
  const fetchCollections = useCallback(async (wsId: string) => {
    setLoadingCollections(true);
    try {
      const res = await fetch(`${API}/collections?workspace_id=${wsId}`);
      if (res.ok) {
        const data = await res.json();
        setCollections(data.collections ?? []);
        const map: Record<string, string> = {};
        for (const c of data.collections ?? []) {
          map[c.id] = c.name;
        }
        setCollectionsMap(map);
      }
    } catch { /* ignore */ } finally {
      setLoadingCollections(false);
    }
  }, []);

  useEffect(() => { fetchWorkspaces(); }, [fetchWorkspaces]);

  useEffect(() => {
    if (selectedWs) {
      fetchEntities(selectedWs, typeFilter || undefined);
      fetchAudit(selectedWs);
      fetchCollections(selectedWs);
    }
  }, [selectedWs, typeFilter, fetchEntities, fetchAudit, fetchCollections]);

  // ── Tipos únicos ─────────────────────────────────────────────────────────
  const entityTypes = [...new Set(entities.map(e => e.type))].sort();

  // ── Filtro local ─────────────────────────────────────────────────────────
  const filtered = searchQuery.trim()
    ? entities.filter(e =>
        e.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        e.type.toLowerCase().includes(searchQuery.toLowerCase()) ||
        e.aliases.some(a => a.toLowerCase().includes(searchQuery.toLowerCase()))
      )
    : entities;

  // ── Estilos reutilizáveis ─────────────────────────────────────────────────
  const s = {
    panel: { display: 'flex', flexDirection: 'column' as const, flex: 1, minHeight: 0, background: '#0f1117', color: '#e2e8f0' },
    toolbar: { display: 'flex', alignItems: 'center', gap: 12, padding: '10px 20px', borderBottom: '1px solid #1e2235', flexShrink: 0 },
    title: { color: '#7c3aed', fontWeight: 700, fontSize: 15 },
    select: { background: '#1e2235', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 6, padding: '5px 10px', fontSize: 13 },
    input: { background: '#1e2235', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 6, padding: '5px 10px', fontSize: 13, outline: 'none' },
    badge: (color: string) => ({ background: color + '22', color, border: `1px solid ${color}44`, borderRadius: 4, padding: '1px 6px', fontSize: 11, fontWeight: 600 }),
    chip: (active: boolean) => ({
      background: active ? '#7c3aed' : '#1e2235',
      color: active ? '#fff' : '#94a3b8',
      border: 'none',
      borderRadius: 6,
      padding: '4px 12px',
      cursor: 'pointer' as const,
      fontSize: 12,
    }),
    card: { background: '#13161f', border: '1px solid #1e2235', borderRadius: 8, padding: '14px 16px' },
  };

  return (
    <div style={s.panel}>

      {/* ── Toolbar ── */}
      <div style={s.toolbar}>
        <span style={s.title}>PKS — Personal Knowledge Store</span>

        <select
          value={selectedWs}
          onChange={e => { setSelectedWs(e.target.value); setSelectedEntity(null); }}
          style={s.select}
        >
          <option value="">Selecione um workspace</option>
          {workspaces.map(ws => (
            <option key={ws.id} value={ws.id}>
              {ws.name} ({ws.id.slice(0, 8)}…)
            </option>
          ))}
        </select>

        <input
          type="text"
          placeholder="Buscar entidades…"
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          style={{ ...s.input, width: 200 }}
        />

        <select
          value={typeFilter}
          onChange={e => setTypeFilter(e.target.value)}
          style={s.select}
        >
          <option value="">Todos os tipos</option>
          {entityTypes.map(t => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>

        <button
          onClick={() => selectedWs && fetchEntities(selectedWs, typeFilter || undefined)}
          style={{ background: '#7c3aed', color: '#fff', border: 'none', borderRadius: 6, padding: '5px 14px', cursor: 'pointer', fontSize: 13 }}
        >
          Atualizar
        </button>

        <span style={{ color: '#64748b', fontSize: 12, marginLeft: 'auto' }}>
          {filtered.length} entidades
        </span>
      </div>

      {/* ── Tabs ── */}
      <div style={{ display: 'flex', gap: 4, padding: '8px 20px', borderBottom: '1px solid #1e2235', flexShrink: 0 }}>
        <button onClick={() => setActiveTab('entities')} style={s.chip(activeTab === 'entities')}>
          Entidades
        </button>
        <button onClick={() => setActiveTab('collections')} style={s.chip(activeTab === 'collections')}>
          Collections
        </button>
        <button onClick={() => setActiveTab('audit')} style={s.chip(activeTab === 'audit')}>
          Audit Log
        </button>
      </div>

      {/* ── Tab: Entidades ── */}
      {activeTab === 'entities' && (
        <div style={{ display: 'flex', flex: 1, minHeight: 0, overflow: 'hidden' }}>

          {/* Lista de entidades */}
          <div style={{ width: 380, borderRight: '1px solid #1e2235', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <div style={{ padding: '8px 12px', borderBottom: '1px solid #1e2235', flexShrink: 0 }}>
              <span style={{ color: '#64748b', fontSize: 11 }}>ENTIDADES DO WORKSPACE</span>
            </div>
            <div style={{ flex: 1, overflowY: 'auto', padding: '8px 8px' }}>
              {loadingEntities ? (
                <div style={{ color: '#7c3aed', textAlign: 'center', padding: 20 }}>Carregando…</div>
              ) : filtered.length === 0 ? (
                <div style={{ color: '#475569', textAlign: 'center', padding: 20, fontSize: 13 }}>Nenhuma entidade encontrada</div>
              ) : (
                filtered.map(entity => {
                  const color = typeColor(entity.type);
                  const sens = sensitivityBadge(entity.sensitivity);
                  const isSelected = selectedEntity?.id === entity.id;
                  return (
                    <div
                      key={entity.id}
                      onClick={() => fetchEntityDetail(entity.id)}
                      style={{
                        ...s.card,
                        marginBottom: 6,
                        cursor: 'pointer',
                        borderLeft: isSelected ? `3px solid ${color}` : '3px solid transparent',
                        background: isSelected ? '#1e2235' : '#13161f',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                        <span style={{
                          width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0,
                          boxShadow: `0 0 4px ${color}`,
                        }} />
                        <span style={{ fontWeight: 600, fontSize: 13, color: '#e2e8f0', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {entity.name}
                        </span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        <span style={{ ...s.badge(color), textTransform: 'uppercase', fontSize: 10 }}>{entity.type}</span>
                        <span style={{ ...s.badge(sens.color), background: sens.bg, color: sens.color, fontSize: 10 }}>{sens.label}</span>
                        <span style={{ color: '#475569', fontSize: 10, marginLeft: 'auto' }} title={entity.last_used_at ? fmtDateTime(entity.last_used_at) : undefined}>
                          {entity.last_used_at ? fmtDate(entity.last_used_at) : '—'}
                        </span>
                      </div>
                      {entity.aliases.length > 0 && (
                        <div style={{ marginTop: 4, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                          {entity.aliases.slice(0, 4).map(a => (
                            <span key={a} style={{ color: '#64748b', fontSize: 10, background: '#1e2235', padding: '1px 5px', borderRadius: 3 }}>{a}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Detail panel */}
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
              {!selectedEntity ? (
                <div style={{ color: '#475569', textAlign: 'center', padding: 40, fontSize: 13 }}>
                  Selecione uma entidade para ver detalhes
                </div>
              ) : loadingDetail ? (
                <div style={{ color: '#7c3aed', textAlign: 'center', padding: 40 }}>Carregando…</div>
              ) : (
                <>
                  {/* Header */}
                  <div style={{ marginBottom: 20 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
                      <span style={{
                        width: 14, height: 14, borderRadius: '50%', background: typeColor(selectedEntity.type),
                        boxShadow: `0 0 8px ${typeColor(selectedEntity.type)}`,
                      }} />
                      <h2 style={{ margin: 0, fontSize: 18, color: '#e2e8f0' }}>{selectedEntity.name}</h2>
                      <span style={{ ...s.badge(typeColor(selectedEntity.type)), textTransform: 'uppercase' }}>{selectedEntity.type}</span>
                      <span style={s.badge(sensitivityBadge(selectedEntity.sensitivity).color)}>{selectedEntity.sensitivity}</span>
                      <button
                        onClick={() => showRaw ? setShowRaw(false) : fetchRawEntity(selectedEntity.id)}
                        style={{
                          background: showRaw ? '#7c3aed' : '#1e2235',
                          color: '#e2e8f0',
                          border: '1px solid #334155',
                          borderRadius: 6,
                          padding: '4px 12px',
                          cursor: 'pointer',
                          fontSize: 12,
                          marginLeft: 'auto',
                        }}
                      >
                        {loadingRaw ? '…' : showRaw ? 'Ocultar RAW' : 'RAW'}
                      </button>
                    </div>
                    {selectedEntity.aliases.length > 0 && (
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
                        {selectedEntity.aliases.map(a => (
                          <span key={a} style={{ color: '#a78bfa', fontSize: 12, background: '#312e81', padding: '2px 8px', borderRadius: 4 }}>{a}</span>
                        ))}
                      </div>
                    )}

                    {showRaw && rawData && (
                      <div style={{ marginTop: 12 }}>
                        <pre style={{
                          background: '#0a0d14',
                          border: '1px solid #334155',
                          borderRadius: 6,
                          padding: 12,
                          fontSize: 11,
                          color: '#a5f3fc',
                          overflowX: 'auto',
                          maxHeight: 300,
                          margin: 0,
                        }}>
                          {JSON.stringify(rawData, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>

                  {/* Metadata grid */}
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 12, marginBottom: 24 }}>
                    {[
                      ['Collection', selectedEntity.collection_id ? (collectionsMap[selectedEntity.collection_id] ?? selectedEntity.collection_id.slice(0, 18) + '…') : '—'],
                      ['Confidence', `${(selectedEntity.confidence * 100).toFixed(0)}%`],
                      ['Source', selectedEntity.source ?? '—'],
                      ['Último uso', fmtDateTime(selectedEntity.last_used_at)],
                      ['Criado em', fmtDateTime(selectedEntity.created_at)],
                      ['Atualizado', fmtDateTime(selectedEntity.updated_at)],
                      ['ID', selectedEntity.id.slice(0, 18) + '…'],
                    ].map(([label, value]) => (
                      <div key={label} style={{ background: '#13161f', border: '1px solid #1e2235', borderRadius: 6, padding: '8px 12px' }}>
                        <div style={{ color: '#64748b', fontSize: 10, marginBottom: 2, textTransform: 'uppercase' }}>{label}</div>
                        <div style={{ color: '#e2e8f0', fontSize: 12, wordBreak: 'break-all' }}>{value}</div>
                      </div>
                    ))}
                  </div>

                  {/* Campos */}
                  {entityFields.length > 0 && (
                    <div style={{ marginBottom: 24 }}>
                      <h3 style={{ color: '#a78bfa', fontSize: 13, marginBottom: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                        Campos ({entityFields.length})
                      </h3>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {entityFields.map(field => {
                          const fv = field.value;
                          const display = typeof fv === 'object' ? JSON.stringify(fv) : String(fv ?? '—');
                          return (
                            <div key={field.id} style={{
                              display: 'flex', alignItems: 'flex-start', gap: 12,
                              background: '#13161f', border: '1px solid #1e2235', borderRadius: 6, padding: '8px 12px',
                            }}>
                              <div style={{ minWidth: 120 }}>
                                <span style={{ color: '#94a3b8', fontSize: 12, fontWeight: 600 }}>{field.key}</span>
                                {field.unit && <span style={{ color: '#64748b', fontSize: 11 }}> ({field.unit})</span>}
                              </div>
                              <div style={{ flex: 1, color: '#e2e8f0', fontSize: 12, wordBreak: 'break-word' }}>{display}</div>
                              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
                                <span style={{ color: '#475569', fontSize: 10 }}>{field.value_type}</span>
                                <span style={{ color: '#64748b', fontSize: 10 }}>{(field.confidence * 100).toFixed(0)}%</span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* Relações */}
                  {entityRelations.length > 0 && (
                    <div>
                      <h3 style={{ color: '#a78bfa', fontSize: 13, marginBottom: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                        Relações ({entityRelations.length})
                      </h3>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {entityRelations.map(rel => {
                          const isFrom = rel.from_entity_id === selectedEntity.id;
                          const otherName = isFrom ? rel.to_name : rel.from_name;
                          const otherType = isFrom ? rel.to_type : rel.from_type;
                          const otherColor = otherType ? typeColor(otherType) : '#94a3b8';
                          const direction = isFrom ? '→' : '←';
                          return (
                            <div key={rel.id} style={{
                              display: 'flex', alignItems: 'center', gap: 10,
                              background: '#13161f', border: '1px solid #1e2235', borderRadius: 6, padding: '8px 12px',
                            }}>
                              <span style={{ color: '#475569', fontSize: 12 }}>{direction}</span>
                              <span style={{ color: otherColor, fontWeight: 600, fontSize: 12 }}>
                                {otherName ?? rel.to_entity_id.slice(0, 8) + '…'}
                              </span>
                              <span style={{ background: '#1e2235', color: '#94a3b8', padding: '1px 6px', borderRadius: 4, fontSize: 10 }}>
                                {rel.relation_type}
                              </span>
                              {otherType && (
                                <span style={{ ...s.badge(otherColor), fontSize: 9, textTransform: 'uppercase' }}>{otherType}</span>
                              )}
                              <span style={{ color: '#475569', fontSize: 10, marginLeft: 'auto' }}>
                                {fmtDate(rel.created_at)}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {entityFields.length === 0 && entityRelations.length === 0 && (
                    <div style={{ color: '#475569', textAlign: 'center', padding: 20, fontSize: 13 }}>
                      Esta entidade não tem campos nem relações
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── Tab: Collections ── */}
      {activeTab === 'collections' && (
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px' }}>
          {loadingCollections ? (
            <div style={{ color: '#7c3aed', textAlign: 'center', padding: 40 }}>Carregando…</div>
          ) : collections.length === 0 ? (
            <div style={{ color: '#475569', textAlign: 'center', padding: 40, fontSize: 13 }}>Nenhuma collection encontrada</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {collections.map(col => (
                <div key={col.id} style={{ ...s.card, borderLeft: '3px solid #7c3aed' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                    <span style={{ color: '#7c3aed', fontWeight: 700, fontSize: 14 }}>{col.name}</span>
                    <span style={{ ...s.badge('#7c3aed'), textTransform: 'uppercase', fontSize: 10 }}>{col.type}</span>
                    <span style={{ color: '#475569', fontSize: 11, marginLeft: 'auto' }}>
                      {fmtDateTime(col.created_at)}
                    </span>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 8 }}>
                    {[
                      ['ID', col.id.slice(0, 18) + '…'],
                      ['Space ID', col.space_id ? col.space_id.slice(0, 18) + '…' : '—'],
                      ['Schema', Object.keys(col.schema).length + ' campos'],
                    ].map(([label, value]) => (
                      <div key={label} style={{ background: '#13161f', border: '1px solid #1e2235', borderRadius: 6, padding: '6px 10px' }}>
                        <div style={{ color: '#64748b', fontSize: 10, marginBottom: 2, textTransform: 'uppercase' }}>{label}</div>
                        <div style={{ color: '#e2e8f0', fontSize: 11, wordBreak: 'break-all' }}>{value}</div>
                      </div>
                    ))}
                  </div>
                  {Object.keys(col.metadata).length > 0 && (
                    <div style={{ marginTop: 8 }}>
                      <div style={{ color: '#64748b', fontSize: 10, marginBottom: 4, textTransform: 'uppercase' }}>Metadata</div>
                      <pre style={{ background: '#0a0d14', border: '1px solid #1e2235', borderRadius: 6, padding: 8, fontSize: 10, color: '#a5f3fc', overflowX: 'auto', margin: 0 }}>
                        {JSON.stringify(col.metadata, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Tab: Audit Log ── */}
      {activeTab === 'audit' && (
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px' }}>
          {loadingAudit ? (
            <div style={{ color: '#7c3aed', textAlign: 'center', padding: 40 }}>Carregando…</div>
          ) : auditLog.length === 0 ? (
            <div style={{ color: '#475569', textAlign: 'center', padding: 40, fontSize: 13 }}>Nenhuma entrada de auditoria</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {auditLog.map(entry => (
                <div key={entry.id} style={{ ...s.card, display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                  {/* Ícone de ação */}
                  <div style={{
                    width: 32, height: 32, borderRadius: 6, flexShrink: 0,
                    background: entry.action.startsWith('upsert') ? '#312e81' : entry.action === 'soft_delete_entity' ? '#9f1239' : '#1e3a5f',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 14,
                  }}>
                    {entry.action === 'upsert_entity' ? '📝' :
                     entry.action === 'upsert_field' ? '🔤' :
                     entry.action === 'upsert_relation' ? '🔗' :
                     entry.action === 'soft_delete_entity' ? '🗑️' :
                     entry.action === 'discard_low_confidence' ? '⏭️' : '•'}
                  </div>

                  {/* Conteúdo */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
                      <span style={{ background: '#1e2235', color: '#e2e8f0', padding: '1px 8px', borderRadius: 4, fontSize: 12, fontWeight: 600 }}>
                        {entry.action}
                      </span>
                      <span style={{ color: '#64748b', fontSize: 11 }}>{entry.target_type}</span>
                      {entry.target_id && (
                        <span style={{ color: '#475569', fontSize: 10 }}>{entry.target_id.slice(0, 12)}…</span>
                      )}
                      {entry.confidence !== null && (
                        <span style={{ color: '#7c3aed', fontSize: 11 }}>{(entry.confidence * 100).toFixed(0)}%</span>
                      )}
                    </div>
                    {entry.reason && (
                      <div style={{ color: '#94a3b8', fontSize: 12, marginBottom: 4 }}>{entry.reason}</div>
                    )}
                    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                      {entry.actor_id && (
                        <span style={{ color: '#64748b', fontSize: 10 }}>actor: {entry.actor_id}</span>
                      )}
                      {entry.source_message_id && (
                        <span style={{ color: '#64748b', fontSize: 10 }}>msg: {entry.source_message_id.slice(0, 8)}…</span>
                      )}
                      <span style={{ color: '#475569', fontSize: 10 }}>{fmtDateTime(entry.created_at)}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}