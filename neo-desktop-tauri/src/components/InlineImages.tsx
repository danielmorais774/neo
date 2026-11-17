// Renderiza as imagens pedidas por `[[IMAGE:…]]` na resposta do Neo: miniatura na
// bolha do chat + lightbox ao clicar. Os bytes vêm do Rust (load_chat_image), do
// disco do usuário ou do catálogo do neo-core — nunca do prompt.
//
// Falha é VISÍVEL de propósito: se o modelo "mostrar" um caminho inventado, o
// cartão diz qual caminho não existe em vez de deixar um quadro quebrado (a
// família de bug do entity_id inventado da skill HA).

import { useCallback, useEffect, useState } from 'react';
import type { InlineImageRef } from '../lib/inlineImages';
import type { ChatImage } from '../lib/tauriApi';
import './InlineImages.css';

interface InlineImagesProps {
  images: InlineImageRef[];
}

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; image: ChatImage }
  | { status: 'error'; message: string };

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Nome legível antes de a imagem carregar (o marcador só traz a origem). */
function labelFor(ref: InlineImageRef): string {
  if (ref.fromCatalog) return 'imagem do catálogo';
  // URL de CDN de loja não tem nome de arquivo útil (`...jpg_720x720q80.webp`,
  // querystring de assinatura): mostra o host, que é a informação que serve.
  if (ref.fromRemote) {
    try {
      return new URL(ref.source).hostname;
    } catch {
      return 'imagem da web';
    }
  }
  const parts = ref.source.split('/');
  return parts[parts.length - 1] || ref.source;
}

function useChatImage(source: string): LoadState {
  // O resultado carrega a origem que o produziu, e "loading" é DERIVADO da
  // divergência — assim trocar de origem não precisa de um setState síncrono
  // dentro do efeito (que dispara render em cascata).
  const [entry, setEntry] = useState<{ source: string; state: LoadState }>({
    source,
    state: { status: 'loading' },
  });

  useEffect(() => {
    let alive = true;
    const api = window.electronAPI;
    const load = api?.loadChatImage
      ? api.loadChatImage(source)
      : Promise.reject(new Error('Este app não sabe carregar imagens (build antigo).'));
    load
      .then((image) => {
        if (alive) setEntry({ source, state: { status: 'ready', image } });
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        if (alive) setEntry({ source, state: { status: 'error', message } });
      });
    return () => {
      alive = false;
    };
  }, [source]);

  return entry.source === source ? entry.state : { status: 'loading' };
}

function InlineImageCard({
  ref_,
  onOpen,
}: {
  ref_: InlineImageRef;
  onOpen: (image: ChatImage) => void;
}) {
  const state = useChatImage(ref_.source);
  const label = labelFor(ref_);

  if (state.status === 'loading') {
    return (
      <div className="inline-image-card inline-image-loading">
        <div className="inline-image-skeleton" />
        <span className="inline-image-name">{label}</span>
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className="inline-image-card inline-image-error">
        <span className="inline-image-error-icon">⚠</span>
        <div className="inline-image-error-body">
          <span className="inline-image-error-msg">Não consegui mostrar a imagem</span>
          <span className="inline-image-error-detail">{state.message}</span>
          <code className="inline-image-error-source">{ref_.source}</code>
        </div>
      </div>
    );
  }

  const { image } = state;
  return (
    <figure className="inline-image-card">
      <button
        type="button"
        className="inline-image-button"
        onClick={() => onOpen(image)}
        title={image.path ? `${image.path} — clique para ampliar` : 'Clique para ampliar'}
      >
        <img className="inline-image-thumb" src={image.dataUrl} alt={ref_.caption || image.name} />
      </button>
      <figcaption className="inline-image-meta">
        <span className="inline-image-name">{ref_.caption || image.name}</span>
        <span className="inline-image-size">{humanSize(image.sizeBytes)}</span>
      </figcaption>
    </figure>
  );
}

function Lightbox({ image, onClose }: { image: ChatImage; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const openExternal = useCallback(() => {
    if (image.path) void window.electronAPI?.revealLocalFile?.(image.path).catch(() => {});
  }, [image.path]);

  return (
    <div className="inline-image-lightbox" onClick={onClose} role="presentation">
      <div className="inline-image-lightbox-inner" onClick={(e) => e.stopPropagation()}>
        {/* clicar na imagem também fecha (é o que o cursor zoom-out promete) */}
        <img src={image.dataUrl} alt={image.name} onClick={onClose} />
        <div className="inline-image-lightbox-bar">
          <span className="inline-image-lightbox-name" title={image.path || image.name}>
            {image.name}
          </span>
          <span className="inline-image-lightbox-size">{humanSize(image.sizeBytes)}</span>
          {image.path && (
            <button type="button" className="inline-image-lightbox-action" onClick={openExternal}>
              Abrir no sistema
            </button>
          )}
          <button type="button" className="inline-image-lightbox-action" onClick={onClose}>
            Fechar
          </button>
        </div>
      </div>
    </div>
  );
}

function InlineImages({ images }: InlineImagesProps) {
  const [zoomed, setZoomed] = useState<ChatImage | null>(null);

  if (!images.length) return null;

  return (
    <div className="inline-image-list">
      {images.map((ref_) => (
        <InlineImageCard key={ref_.source} ref_={ref_} onOpen={setZoomed} />
      ))}
      {zoomed && <Lightbox image={zoomed} onClose={() => setZoomed(null)} />}
    </div>
  );
}

export default InlineImages;
