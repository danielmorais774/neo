import { useEffect, useState } from 'react';
import { isModelCatalog, readCachedCatalog, storeCatalog, type ModelCatalog } from '../lib/models';

// Enquanto o core não respondeu nenhuma vez, tenta de novo neste intervalo.
const RETRY_MS = 15_000;
// Ao voltar o foco para a janela relê o catálogo (pega o que mudou no painel),
// mas não mais que uma vez por este intervalo.
const FOCUS_REFRESH_MS = 60_000;

/**
 * Catálogo do seletor do chat: nasce do cache (abre na hora) e é atualizado pelo
 * neo-core por baixo. `null` só quando nunca houve resposta nem cache.
 */
export function useModelCatalog(): ModelCatalog | null {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(readCachedCatalog);

  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let lastFetch = 0;

    const fetchCatalog = async () => {
      lastFetch = Date.now();
      try {
        const fresh: unknown = await window.electronAPI?.getModelCatalog?.();
        if (cancelled) return;
        if (!isModelCatalog(fresh)) throw new Error('resposta sem o formato do catálogo');
        storeCatalog(fresh);
        setCatalog(fresh);
      } catch (err) {
        if (cancelled) return;
        console.warn('[models] catálogo do neo-core indisponível, seguindo com o cache:', err);
        retry = setTimeout(fetchCatalog, RETRY_MS);
      }
    };

    const onFocus = () => {
      if (Date.now() - lastFetch < FOCUS_REFRESH_MS) return;
      clearTimeout(retry);
      void fetchCatalog();
    };

    void fetchCatalog();
    window.addEventListener('focus', onFocus);
    return () => {
      cancelled = true;
      clearTimeout(retry);
      window.removeEventListener('focus', onFocus);
    };
  }, []);

  return catalog;
}
