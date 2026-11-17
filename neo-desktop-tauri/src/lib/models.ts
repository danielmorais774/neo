// Catálogo de providers/modelos do seletor do chat — vem do neo-core.
//
// Antes isto era um enum copiado à mão aqui e no chat do painel, e os dois já
// tinham divergido do core (ofereciam um Claude que o core não conhecia). Agora
// a fonte é `GET /api/v1/desktop/model-catalog` (editado no painel em
// Configurações → Providers), via o comando Rust `get_model_catalog`.
//
// A última lista recebida fica no localStorage: o seletor abre na hora com ela
// e a busca no core só atualiza por baixo (hooks/useModelCatalog). Sem cache e
// sem core não há lista — e também não há chat, então não existe lista reserva.

export type ProviderType = string;
export type ModelType = string;

export interface CatalogOption<T extends string> {
  value: T;
  label: string;
  description: string;
}

export interface CatalogModel {
  id: string;
  label: string;
  description: string;
  vision: boolean;
}

export interface CatalogProvider {
  name: string;
  label: string;
  description: string;
  models: CatalogModel[];
}

export interface ModelCatalog {
  providers: CatalogProvider[];
  /** O par que vale quando não há escolha salva (o DEFAULT_PROVIDER do core). */
  default: { provider: string; model: string } | null;
}

export interface Selection {
  provider: string;
  model: string;
}

const CACHE_KEY = 'neo.chat.catalog';

/** O JSON tem a forma que o seletor precisa? (cache velho ou resposta estranha) */
export function isModelCatalog(value: unknown): value is ModelCatalog {
  if (!value || typeof value !== 'object') return false;
  const providers = (value as ModelCatalog).providers;
  return (
    Array.isArray(providers) &&
    providers.every(
      (p) =>
        p &&
        typeof p.name === 'string' &&
        Array.isArray(p.models) &&
        p.models.every((m) => m && typeof m.id === 'string')
    )
  );
}

export function readCachedCatalog(): ModelCatalog | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isModelCatalog(parsed) ? parsed : null;
  } catch {
    // localStorage pode lançar (storage bloqueado) e o JSON pode estar corrompido.
    return null;
  }
}

export function storeCatalog(catalog: ModelCatalog): void {
  try {
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(catalog));
  } catch {
    /* cache é conveniência: falhar em salvar não pode derrubar o app */
  }
}

export function providerOptions(catalog: ModelCatalog): CatalogOption<ProviderType>[] {
  return catalog.providers.map((p) => ({
    value: p.name,
    label: p.label || p.name,
    description: p.description,
  }));
}

export function modelOptions(catalog: ModelCatalog, provider: string): CatalogOption<ModelType>[] {
  const found = catalog.providers.find((p) => p.name === provider);
  return (found?.models ?? []).map((m) => ({
    value: m.id,
    label: m.label || m.id,
    description: m.description,
  }));
}

/** Primeiro modelo do provider — o que o seletor escolhe ao trocar de provider. */
export function defaultModelFor(catalog: ModelCatalog, provider: string): ModelType {
  return catalog.providers.find((p) => p.name === provider)?.models[0]?.id ?? '';
}

/**
 * O par salvo, corrigido contra o catálogo.
 *
 * Provider que sumiu da lista (desligado, sem chave) cai no default do core; um
 * modelo que não pertence mais ao provider cai no primeiro dele. Sem isso o
 * GlassSelect mostraria o primeiro item enquanto o estado (e o que vai para o
 * neo-core) apontava para outro.
 */
export function resolveSelection(catalog: ModelCatalog, wanted: Selection): Selection {
  const provider = catalog.providers.find((p) => p.name === wanted.provider);
  if (provider) {
    const model = provider.models.some((m) => m.id === wanted.model)
      ? wanted.model
      : provider.models[0]?.id ?? '';
    return { provider: provider.name, model };
  }
  if (catalog.default) return catalog.default;
  const first = catalog.providers[0];
  return { provider: first?.name ?? '', model: first?.models[0]?.id ?? '' };
}
