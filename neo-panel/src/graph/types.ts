/*
 * Formato que /api/v1/knowledge-graph/graph devolve HOJE.
 *
 * O painel antigo ainda falava `user_id` e `properties` — campos que o PKS não
 * emite mais (viraram `workspace_id` e `metadata`). Ler `properties` de um
 * payload atual dava `Object.keys(undefined)` e derrubava a árvore inteira no
 * clique. Por isso tudo que vem da rede passa por `normalize*` antes de entrar
 * no estado: o resto do arquivo pode confiar nos tipos.
 */

export interface GraphEntity {
  id: string;
  type: string;
  name: string;
  workspace_id: string | null;
  aliases: string[];
  metadata: Record<string, unknown>;
  confidence: number;
  sensitivity: string;
  created_at: string | null;
}

export interface GraphRelation {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
  workspace_id: string | null;
  metadata: Record<string, unknown>;
  confidence: number;
  sensitivity: string;
}

export interface GraphPayload {
  entities: GraphEntity[];
  relations: GraphRelation[];
}

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
const obj = (v: unknown): Record<string, unknown> =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

export function normalizeEntity(raw: Record<string, unknown>): GraphEntity {
  return {
    id: str(raw.id),
    type: str(raw.type, 'desconhecido'),
    name: str(raw.name, '(sem nome)'),
    // `user_id` é o nome antigo; aceitar os dois evita um painel vazio caso o
    // neo-core em produção esteja atrás do que está no repo.
    workspace_id: str(raw.workspace_id) || str(raw.user_id) || null,
    aliases: Array.isArray(raw.aliases) ? raw.aliases.filter((a): a is string => typeof a === 'string') : [],
    metadata: Object.keys(obj(raw.metadata)).length ? obj(raw.metadata) : obj(raw.properties),
    confidence: num(raw.confidence),
    sensitivity: str(raw.sensitivity, 'private'),
    created_at: str(raw.created_at) || null,
  };
}

export function normalizeRelation(raw: Record<string, unknown>): GraphRelation {
  return {
    id: str(raw.id),
    source_id: str(raw.source_id) || str(raw.from_entity_id),
    target_id: str(raw.target_id) || str(raw.to_entity_id),
    type: str(raw.type) || str(raw.relation_type, 'relacionado'),
    workspace_id: str(raw.workspace_id) || null,
    metadata: obj(raw.metadata),
    confidence: num(raw.confidence),
    sensitivity: str(raw.sensitivity, 'private'),
  };
}

export function normalizePayload(raw: unknown): GraphPayload {
  const src = obj(raw);
  const entities = (Array.isArray(src.entities) ? src.entities : []).map((e) => normalizeEntity(obj(e)));
  const known = new Set(entities.map((e) => e.id));
  const relations = (Array.isArray(src.relations) ? src.relations : [])
    .map((r) => normalizeRelation(obj(r)))
    // O endpoint corta entidades em 500 e relações em 1000, então sobram arestas
    // apontando pra fora do corte. Elas viravam `null` no meio do render; é mais
    // barato removê-las aqui, uma vez, do que testar a cada quadro.
    .filter((r) => known.has(r.source_id) && known.has(r.target_id));
  return { entities, relations };
}
