import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { GraphEngine } from './graph/engine';
import { ALL_FAMILIES, colorOf, familyIndex, FAMILIES, familyOf } from './graph/palette';
import { normalizePayload, type GraphEntity, type GraphPayload, type GraphRelation } from './graph/types';
import './graph/graph.css';

const API = '/api/v1/knowledge-graph';

/* Detalhe da entidade — /entities/{id} devolve os campos tipados (pks_entity_field)
 * e as relações já com o NOME das duas pontas. O painel antigo tinha esse
 * endpoint disponível e nunca chamava: mostrava só o que vinha no /graph. */
interface EntityField {
  id: string;
  key: string;
  value: unknown;
  unit: string | null;
  confidence: number;
}
interface EntityRelation {
  id: string;
  relation_type: string;
  from_entity_id: string;
  to_entity_id: string;
  from_name: string;
  to_name: string;
  from_type: string;
  to_type: string;
}
interface EntityDetail {
  entity: GraphEntity | null;
  fields: EntityField[];
  relations: EntityRelation[];
}

/* Campos do PKS guardam desde "Daniel" até o JSON inteiro do estado afetivo.
 * O cartão mostra o começo e deixa o resto no title — despejar 2 KB de JSON
 * empurrava o resto da ficha pra fora da tela. */
const MAX_VALUE = 220;
const fmtValue = (v: unknown): string => {
  if (v === null || v === undefined) return '—';
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
  return s.length > MAX_VALUE ? `${s.slice(0, MAX_VALUE)}…` : s;
};
const fullValue = (v: unknown): string =>
  v === null || v === undefined ? '—' : typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);

export default function KnowledgeGraphPanel() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<GraphEngine | null>(null);

  const [data, setData] = useState<GraphPayload>({ entities: [], relations: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [userIds, setUserIds] = useState<string[]>([]);
  const [selectedUser, setSelectedUser] = useState('');

  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EntityDetail | null>(null);
  const [settled, setSettled] = useState(false);

  const [offFamilies, setOffFamilies] = useState<Set<string>>(new Set());
  const [offTypes, setOffTypes] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [pulses, setPulses] = useState(true);
  const [labels, setLabels] = useState(true);

  const byId = useMemo(() => new Map(data.entities.map((e) => [e.id, e])), [data.entities]);

  // ── Rede ──────────────────────────────────────────────────────────────────

  const fetchGraph = useCallback(async (uid?: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(uid ? `${API}/graph?user_id=${encodeURIComponent(uid)}` : `${API}/graph`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(normalizePayload(await res.json()));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro ao buscar o grafo');
      setData({ entities: [], relations: [] });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetch(`${API}/users`)
      .then((r) => r.json())
      .then((d) => setUserIds(Array.isArray(d?.user_ids) ? d.user_ids : []))
      .catch(() => setUserIds([]));
    fetchGraph();
  }, [fetchGraph]);

  // Detalhe: só ao selecionar, e cancelado se a seleção mudar no meio.
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetail(null);
    fetch(`${API}/entities/${selectedId}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (alive && d) setDetail(d as EntityDetail);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [selectedId]);

  // ── Motor ─────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!canvasRef.current) return;
    const engine = new GraphEngine(canvasRef.current, {
      onHover: setHoveredId,
      onSelect: setSelectedId,
      onSettle: setSettled,
    });
    engineRef.current = engine;
    return () => {
      engine.destroy();
      engineRef.current = null;
    };
  }, []);

  useEffect(() => {
    engineRef.current?.setData(
      data.entities,
      data.relations,
      familyIndex,
      FAMILIES.length + 1,
      colorOf,
    );
  }, [data]);

  useEffect(() => {
    engineRef.current?.setOptions({ showPulses: pulses, showLabels: labels });
  }, [pulses, labels]);

  // ── Filtros ───────────────────────────────────────────────────────────────

  const typesPresent = useMemo(() => {
    const byFam = new Map<string, string[]>();
    for (const fam of ALL_FAMILIES) byFam.set(fam.id, []);
    for (const t of new Set(data.entities.map((e) => e.type))) {
      byFam.get(familyOf(t).id)!.push(t);
    }
    for (const list of byFam.values()) list.sort();
    return byFam;
  }, [data.entities]);

  const isHiddenType = useCallback(
    (type: string) => offFamilies.has(familyOf(type).id) || offTypes.has(type),
    [offFamilies, offTypes],
  );

  useEffect(() => {
    const hidden = new Set<string>();
    for (const e of data.entities) if (isHiddenType(e.type)) hidden.add(e.id);
    engineRef.current?.setHidden(hidden);
  }, [data.entities, isHiddenType]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return null;
    const hit = data.entities.filter(
      (e) =>
        e.name.toLowerCase().includes(q) ||
        e.type.toLowerCase().includes(q) ||
        e.aliases.some((a) => a.toLowerCase().includes(q)),
    );
    return hit;
  }, [query, data.entities]);

  useEffect(() => {
    engineRef.current?.setSearchFocus(matches ? new Set(matches.map((e) => e.id)) : null);
  }, [matches]);

  const toggle = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };

  // ── Derivados da UI ───────────────────────────────────────────────────────

  const visibleCount = useMemo(
    () => data.entities.filter((e) => !isHiddenType(e.type)).length,
    [data.entities, isHiddenType],
  );
  const visibleRelations = useMemo(
    () =>
      data.relations.filter((r: GraphRelation) => {
        const a = byId.get(r.source_id);
        const b = byId.get(r.target_id);
        return a && b && !isHiddenType(a.type) && !isHiddenType(b.type);
      }).length,
    [data.relations, byId, isHiddenType],
  );

  const hovered = hoveredId ? byId.get(hoveredId) : null;
  const selectedEntity = detail?.entity ?? (selectedId ? byId.get(selectedId) ?? null : null);
  const empty = !loading && !error && data.entities.length === 0;

  const goTo = (id: string) => {
    engineRef.current?.select(id);
    engineRef.current?.centerOn(id);
    setSelectedId(id);
  };

  return (
    <div className="kg-root">
      <div className="kg-bar">
        <select
          className="kg-bar-select"
          value={selectedUser}
          onChange={(e) => {
            setSelectedUser(e.target.value);
            fetchGraph(e.target.value || undefined);
          }}
        >
          <option value="">Todos os workspaces</option>
          {userIds.map((uid) => (
            <option key={uid} value={uid}>
              {uid.slice(0, 8)}…
            </option>
          ))}
        </select>

        <button className="kg-btn" onClick={() => fetchGraph(selectedUser || undefined)} disabled={loading}>
          {loading ? 'Lendo…' : 'Atualizar'}
        </button>

        <input
          className="kg-search"
          placeholder="Buscar por nome, apelido ou tipo…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && matches?.length) goTo(matches[0].id);
            if (e.key === 'Escape') setQuery('');
          }}
        />

        <span className="kg-count">
          {visibleCount === data.entities.length
            ? `${data.entities.length} entidades · ${data.relations.length} relações`
            : `${visibleCount}/${data.entities.length} entidades · ${visibleRelations}/${data.relations.length} relações`}
          {matches && ` · ${matches.length} na busca`}
        </span>

        <button className={`kg-toggle${pulses ? ' is-on' : ''}`} onClick={() => setPulses((v) => !v)}>
          Sinapses
        </button>
        <button className={`kg-toggle${labels ? ' is-on' : ''}`} onClick={() => setLabels((v) => !v)}>
          Nomes
        </button>

        <span className="kg-hint">Roda = zoom · arraste o fundo = mover · arraste um nó = reposicionar</span>
      </div>

      {data.entities.length > 0 && (
        <div className="kg-legend">
          {ALL_FAMILIES.map((fam) => {
            const types = typesPresent.get(fam.id) ?? [];
            if (!types.length) return null;
            const famOff = offFamilies.has(fam.id);
            return (
              <div key={fam.id} className={`kg-fam${famOff ? ' is-off' : ''}`}>
                <button
                  className="kg-fam-head"
                  onClick={() => setOffFamilies((s) => toggle(s, fam.id))}
                  title={famOff ? 'Mostrar esta família' : 'Esconder esta família'}
                >
                  <span className="kg-dot" style={{ background: fam.color, color: fam.color }} />
                  {fam.label}
                </button>
                <div className="kg-fam-types">
                  {types.map((t) => (
                    <button
                      key={t}
                      className={`kg-type${famOff || offTypes.has(t) ? ' is-off' : ''}`}
                      onClick={() => setOffTypes((s) => toggle(s, t))}
                      title={`Tipo ${t}`}
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="kg-stage">
        <canvas ref={canvasRef} className="kg-canvas" />

        {loading && (
          <div className="kg-status">
            <div className="kg-spinner" />
            <span style={{ color: 'var(--neo-text-muted)', fontSize: 13 }}>Lendo o PKS…</span>
          </div>
        )}
        {!loading && error && (
          <div className="kg-status is-error">
            <span>{error}</span>
            <button className="kg-btn" onClick={() => fetchGraph(selectedUser || undefined)}>
              Tentar de novo
            </button>
          </div>
        )}
        {empty && (
          <div className="kg-status">
            <span style={{ color: 'var(--neo-text-faint)', fontSize: 13 }}>
              Nenhuma entidade no grafo de conhecimento ainda.
            </span>
          </div>
        )}

        {!loading && !error && !settled && data.entities.length > 0 && (
          <div className="kg-settling">
            <span className="kg-dot" style={{ background: 'var(--neo-violet)', color: 'var(--neo-violet)' }} />
            acomodando a rede…
          </div>
        )}

        {hovered && hovered.id !== selectedId && (
          <div className="kg-hud">
            <span className="kg-dot" style={{ background: colorOf(hovered.type), color: colorOf(hovered.type) }} />
            <span>{hovered.name}</span>
            <small>{hovered.type}</small>
          </div>
        )}

        {selectedEntity && (
          <aside className="kg-detail">
            <div className="kg-detail-head">
              <span
                className="kg-dot"
                style={{ background: colorOf(selectedEntity.type), color: colorOf(selectedEntity.type), marginTop: 5 }}
              />
              <div className="kg-detail-name">
                {selectedEntity.name}
                <div className="kg-detail-type">
                  {selectedEntity.type} · {familyOf(selectedEntity.type).label}
                </div>
              </div>
              <button
                className="kg-close"
                onClick={() => {
                  engineRef.current?.select(null);
                  setSelectedId(null);
                }}
              >
                ×
              </button>
            </div>

            <div className="kg-detail-body">
              <div className="kg-sec">Ficha</div>
              <div className="kg-kv">
                <b>Confiança</b>
                <span>{(selectedEntity.confidence * 100).toFixed(0)}%</span>
              </div>
              <div className="kg-meter">
                <i
                  style={{
                    width: `${Math.min(100, Math.max(0, selectedEntity.confidence * 100))}%`,
                    background: colorOf(selectedEntity.type),
                  }}
                />
              </div>
              <div className="kg-kv">
                <b>Sensibilidade</b>
                <span>{selectedEntity.sensitivity}</span>
              </div>
              {selectedEntity.created_at && (
                <div className="kg-kv">
                  <b>Criado</b>
                  <span>{new Date(selectedEntity.created_at).toLocaleString('pt-BR')}</span>
                </div>
              )}

              {selectedEntity.aliases.length > 0 && (
                <>
                  <div className="kg-sec">Também chamado de</div>
                  {selectedEntity.aliases.map((a) => (
                    <span key={a} className="kg-chip">
                      {a}
                    </span>
                  ))}
                </>
              )}

              {detail && detail.fields.length > 0 && (
                <>
                  <div className="kg-sec">Campos</div>
                  {detail.fields.map((f) => (
                    <div key={f.id} className="kg-kv">
                      <b>{f.key}</b>
                      <span title={fullValue(f.value)}>
                        {fmtValue(f.value)}
                        {f.unit ? ` ${f.unit}` : ''}
                      </span>
                    </div>
                  ))}
                </>
              )}

              {Object.keys(selectedEntity.metadata).length > 0 && (
                <>
                  <div className="kg-sec">Metadados</div>
                  {Object.entries(selectedEntity.metadata).map(([k, v]) => (
                    <div key={k} className="kg-kv">
                      <b>{k}</b>
                      <span title={fullValue(v)}>{fmtValue(v)}</span>
                    </div>
                  ))}
                </>
              )}

              {detail && detail.relations.length > 0 && (
                <>
                  <div className="kg-sec">Ligações ({detail.relations.length})</div>
                  {detail.relations.map((r) => {
                    const outgoing = r.from_entity_id === selectedEntity.id;
                    const peerId = outgoing ? r.to_entity_id : r.from_entity_id;
                    const peerName = outgoing ? r.to_name : r.from_name;
                    const peerType = outgoing ? r.to_type : r.from_type;
                    return (
                      <div key={r.id} className="kg-rel">
                        <span className="kg-rel-type">{outgoing ? '→' : '←'} {r.relation_type}</span>
                        <button
                          className="kg-rel-peer"
                          onClick={() => goTo(peerId)}
                          disabled={!byId.has(peerId)}
                          title={byId.has(peerId) ? 'Ir para esta entidade' : 'Fora do recorte carregado'}
                          style={{ color: colorOf(peerType) }}
                        >
                          {peerName}
                        </button>
                      </div>
                    );
                  })}
                </>
              )}

              <div className="kg-id">{selectedEntity.id}</div>
            </div>
          </aside>
        )}

        <div className="kg-zoom">
          <button onClick={() => engineRef.current?.zoomBy(1.25)} title="Aproximar">
            +
          </button>
          <button onClick={() => engineRef.current?.zoomBy(0.8)} title="Afastar">
            −
          </button>
          <button onClick={() => engineRef.current?.fit()} title="Enquadrar tudo">
            ⤢
          </button>
        </div>
      </div>
    </div>
  );
}
