// Visor do caderno de trabalho do Neo — read-only, estilo preview de .md.
//
// Por que esta tela existe: a memória de trabalho
// (neo-core/docs/working-memory-plan.md) nasceu sem janela nenhuma. Até aqui,
// ver o que o Neo anota exigia `psql` na VPS ou pedir `wm_read` no chat — e
// perguntar ao modelo o que ele escreveu é a pior fonte possível, porque a
// resposta passa pelo próprio componente sob suspeita.
//
// Read-only de propósito: `pinned` e `status` decidem o que entra no índice
// injetado no prompt de TODO turno. Mexer nisso muda o comportamento do Neo, e
// isso não pode ser efeito colateral de abrir uma tela para olhar.
//
// O corpo vem em duas etapas (lista sem `body`, corpo no clique) porque as
// notas somam centenas de KB de markdown: carregar tudo de uma vez tornaria a
// abertura da tela lenta para mostrar exatamente um item.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github-dark.css';
import type { WorkingNoteDetail, WorkingNoteSummary } from '../lib/tauriApi';
import { humanizeTime } from '../hooks/useSessionManagement';
import CodeBlock, { CopyButton } from './CodeBlock';
import './WorkingMemoryViewer.css';

interface WorkingMemoryViewerProps {
  /** Fecha a tela. Na janela própria, fecha a JANELA (ver App.tsx). */
  onClose: () => void;
  /**
   * `true` quando a tela é a janela inteira, e não um overlay por cima do chat.
   * Muda três coisas: some o backdrop (não há nada atrás para escurecer), some o
   * portal (o componente já É a raiz da página) e some o botão de fechar — a
   * decoração nativa da janela já tem o dela, e dois botões de fechar na mesma
   * barra é a UI dizendo coisas diferentes sobre o mesmo gesto.
   */
  standalone?: boolean;
}

type LoadState = 'loading' | 'ready' | 'error';

/** `2026-09-09T21:07:14` (UTC, sem sufixo) → data/hora local legível. */
function formatStamp(iso: string | null): string {
  if (!iso) return '—';
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Dias até o GC arquivar a nota. `null` quando fixada (expires_at fica NULL). */
function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) return null;
  return Math.round((date.getTime() - Date.now()) / 86_400_000);
}

export default function WorkingMemoryViewer({ onClose, standalone = false }: WorkingMemoryViewerProps) {
  const [notes, setNotes] = useState<WorkingNoteSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [listState, setListState] = useState<LoadState>('loading');
  const [listError, setListError] = useState<string>('');

  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkingNoteDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>('loading');
  const [detailError, setDetailError] = useState<string>('');

  const [query, setQuery] = useState('');
  // Sobe a cada clique em "recarregar". Entra nas dependências do efeito que lê
  // o corpo porque a nota ABERTA também precisa ser refeita: o caderno é
  // escrito pelo Neo enquanto a tela está de pé (o backstop grava um append por
  // turno), então recarregar só a lista mostraria uma contagem de entradas nova
  // ao lado de um corpo velho.
  const [reloadTick, setReloadTick] = useState(0);
  const bodyRef = useRef<HTMLDivElement>(null);

  const loadList = useCallback(async () => {
    setListState('loading');
    setListError('');
    try {
      const page = await window.electronAPI?.listWorkingNotes();
      const list = page?.notes ?? [];
      setNotes(list);
      setCounts(page?.counts ?? {});
      setListState('ready');
      // Seleciona a nota mais recente sozinho: abrir numa tela vazia obrigaria
      // um clique só para descobrir do que a tela se trata.
      setSelected((current) => current ?? list[0]?.slug ?? null);
    } catch (err) {
      console.error('[Neo] Falha ao carregar o caderno:', err);
      setListError(String(err));
      setListState('error');
    }
  }, []);

  const reload = useCallback(() => {
    setReloadTick((tick) => tick + 1);
    void loadList();
  }, [loadList]);

  // A busca inicial vive numa função aninhada pelo mesmo motivo do efeito de
  // baixo: `loadList` mexe em estado, e chamá-la direto no corpo do efeito é o
  // padrão de renderização em cascata que a regra de hooks reprova.
  useEffect(() => {
    void (async () => {
      await loadList();
    })();
  }, [loadList]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;

    // Todo setState mora dentro da função assíncrona, e não no corpo do efeito:
    // é o que a regra `react-hooks/set-state-in-effect` pede, e de quebra o
    // "Abrindo a nota…" e o resultado passam a sair do mesmo lugar.
    const open = async () => {
      setDetailState('loading');
      setDetailError('');
      try {
        const note = await window.electronAPI?.readWorkingNote(selected);
        if (cancelled) return;
        setDetail(note ?? null);
        setDetailState('ready');
      } catch (err) {
        if (cancelled) return;
        console.error('[Neo] Falha ao ler a nota:', err);
        setDetailError(String(err));
        setDetailState('error');
      }
    };

    void open();

    return () => {
      cancelled = true;
    };
  }, [selected, reloadTick]);

  // Volta ao topo ao trocar de nota: sem isto, abrir uma nota curta depois de
  // rolar uma longa mostra o painel em branco no meio do scroll herdado.
  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [selected, detail]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return notes;
    return notes.filter((note) =>
      `${note.slug} ${note.title} ${note.summary}`.toLowerCase().includes(term)
    );
  }, [notes, query]);

  const autoCount = (counts['auto/active'] ?? 0) + (counts['auto/done'] ?? 0);
  const modelCount = (counts['model/active'] ?? 0) + (counts['model/done'] ?? 0);
  const getBody = useCallback(() => detail?.body ?? '', [detail]);

  const screen = (
    <section
      className={`wm-screen${standalone ? ' wm-standalone' : ''}`}
      role={standalone ? 'region' : 'dialog'}
      aria-modal={standalone ? undefined : true}
      aria-labelledby="wm-title"
      onMouseDown={standalone ? undefined : (event) => event.stopPropagation()}
    >
      <header className="wm-header">
        <div className="wm-header-copy">
          <h2 id="wm-title">Memória de trabalho</h2>
          <p>
            O caderno da tarefa atual do Neo — rascunho que expira, não o PKS.
            {listState === 'ready' && notes.length > 0 && (
              <>
                {' '}
                <strong>{notes.length}</strong> notas ·{' '}
                <span
                  className="wm-origin-split"
                  title="Notas escritas pelo próprio Neo (wm_write) contra as do backstop automático do turno"
                >
                  {modelCount} do Neo · {autoCount} automáticas
                </span>
              </>
            )}
          </p>
        </div>
        <div className="wm-header-actions">
          <button
            type="button"
            className="wm-icon-btn"
            onClick={reload}
            title="Recarregar do neo-core"
            aria-label="Recarregar"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
              <polyline points="3 3 3 8 8 8" />
            </svg>
          </button>
          {!standalone && (
            <button
              type="button"
              className="wm-icon-btn"
              onClick={onClose}
              title="Fechar"
              aria-label="Fechar"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          )}
        </div>
      </header>

      <div className="wm-columns">
        <aside className="wm-list-pane" aria-label="Notas do caderno">
          <div className="wm-search">
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filtrar por nome ou resumo"
              aria-label="Filtrar notas"
            />
          </div>

          {listState === 'loading' ? (
            <div className="wm-state">Carregando o caderno…</div>
          ) : listState === 'error' ? (
            <div className="wm-state wm-state-error">
              Não foi possível falar com o neo-core.
              <code>{listError}</code>
              <button type="button" className="wm-retry" onClick={() => void loadList()}>
                Tentar de novo
              </button>
            </div>
          ) : notes.length === 0 ? (
            <div className="wm-state">
              O caderno está vazio. O Neo grava aqui quando conclui algo que vai
              fazer falta no turno seguinte.
            </div>
          ) : filtered.length === 0 ? (
            <div className="wm-state">Nenhuma nota casa com “{query}”.</div>
          ) : (
            <ul className="wm-list">
              {filtered.map((note) => {
                const isCurrent = note.slug === selected;
                const expiring = daysUntil(note.expires_at);
                return (
                  <li key={note.slug}>
                    <button
                      type="button"
                      className={`wm-item${isCurrent ? ' current' : ''}`}
                      onClick={() => setSelected(note.slug)}
                      aria-current={isCurrent}
                    >
                      <span className="wm-item-top">
                        <span className="wm-item-title">{note.title || note.slug}</span>
                        {note.pinned && (
                          <span className="wm-badge wm-badge-pin" title="Fixada — não expira">
                            fixada
                          </span>
                        )}
                      </span>
                      <span className="wm-item-slug">[[{note.slug}]]</span>
                      {note.summary && <span className="wm-item-summary">{note.summary}</span>}
                      <span className="wm-item-meta">
                        <span
                          className={`wm-badge wm-badge-${note.origin === 'auto' ? 'auto' : 'model'}`}
                          title={
                            note.origin === 'auto'
                              ? 'Backstop automático: o Neo não anotou por conta, o turno foi registrado sozinho'
                              : 'O Neo chamou wm_write por conta própria'
                          }
                        >
                          {note.origin === 'auto' ? 'auto' : 'do Neo'}
                        </span>
                        {note.status !== 'active' && (
                          <span className="wm-badge wm-badge-done" title="Arquivada: fora do índice do prompt">
                            arquivada
                          </span>
                        )}
                        <span className="wm-item-dot">·</span>
                        {note.entry_count} {note.entry_count === 1 ? 'entrada' : 'entradas'}
                        <span className="wm-item-dot">·</span>
                        {humanizeTime(note.updated_at)}
                        {expiring !== null && expiring <= 3 && (
                          <>
                            <span className="wm-item-dot">·</span>
                            <span className="wm-expiring" title="O GC arquiva quando vencer">
                              {expiring <= 0 ? 'vencendo' : `expira em ${expiring}d`}
                            </span>
                          </>
                        )}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <div className="wm-preview-pane">
          {!selected ? (
            <div className="wm-state">Escolha uma nota à esquerda.</div>
          ) : detailState === 'loading' ? (
            <div className="wm-state">Abrindo a nota…</div>
          ) : detailState === 'error' ? (
            <div className="wm-state wm-state-error">
              {detailError}
              <button
                type="button"
                className="wm-retry"
                onClick={reload}
                title="Recarrega a lista — a nota pode ter expirado"
              >
                Recarregar a lista
              </button>
            </div>
          ) : detail ? (
            <>
              <div className="wm-preview-bar">
                <div className="wm-preview-facts">
                  <code>[[{detail.slug}]]</code>
                  <span>criada {formatStamp(detail.created_at)}</span>
                  <span>tocada {formatStamp(detail.updated_at)}</span>
                  <span>
                    {detail.pinned
                      ? 'fixada (não expira)'
                      : `expira ${formatStamp(detail.expires_at)}`}
                  </span>
                </div>
                <CopyButton getText={getBody} className="wm-copy" />
              </div>

              {detail.links.length > 0 && (
                <div className="wm-links" aria-label="Links da nota">
                  {detail.links.map((link) => {
                    // `[[outra-nota]]` navega; `file:`/`path:` são só rótulo —
                    // o visor não abre arquivo nem sandbox.
                    const target = notes.find((note) => note.slug === link);
                    return target ? (
                      <button
                        key={link}
                        type="button"
                        className="wm-link wm-link-nav"
                        onClick={() => setSelected(link)}
                        title="Abrir esta nota"
                      >
                        [[{link}]]
                      </button>
                    ) : (
                      <span key={link} className="wm-link">
                        {link}
                      </span>
                    );
                  })}
                </div>
              )}

              <div className="wm-md" ref={bodyRef}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  rehypePlugins={[rehypeHighlight]}
                  components={{ pre: CodeBlock }}
                >
                  {detail.body || '_Nota sem corpo._'}
                </ReactMarkdown>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </section>
  );

  // Na janela própria não há portal nem backdrop: o componente é a página.
  // Como overlay, continua saindo pelo portal em document.body — o `.app` tem
  // overflow:hidden e uma máscara radial que recortariam a tela por dentro.
  if (standalone) return screen;

  return createPortal(
    <div className="wm-backdrop" role="presentation" onMouseDown={onClose}>
      {screen}
    </div>,
    document.body,
  );
}
