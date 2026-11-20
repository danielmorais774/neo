import { useEffect, useState } from 'react';
import { applySaved } from './lib/headlessLimits';
import { ChatPage, QuickHelpPage } from './pages';
import { WindowService } from './lib';
import type { WindowResizeDirection } from './lib';
import { DevicesTab } from './components/DevicesTab';
import SettingsModal from './components/SettingsModal';
import SessionsSidebar from './components/SessionsSidebar';
import ConversationsPanel from './components/ConversationsPanel';
import AgentActivityPanel from './components/AgentActivityPanel';
import ConversationHeader from './components/ConversationHeader';
import JobsColumn from './components/JobsColumn';
import { useWideLayout } from './hooks/useWideLayout';
import { readColumnOpen, storeColumnOpen } from './lib/chatPrefs';
import { useTheme } from './themes/useTheme';
import './App.css';
import './index.css';
import './styles/themes.css';

const _urlMode = new URLSearchParams(window.location.search).get('mode');

const RESIZE_HANDLES: { direction: WindowResizeDirection; className: string; label: string }[] = [
  { direction: 'North', className: 'resize-handle-n', label: 'Redimensionar pelo topo' },
  { direction: 'South', className: 'resize-handle-s', label: 'Redimensionar pela base' },
  { direction: 'West', className: 'resize-handle-w', label: 'Redimensionar pela esquerda' },
  { direction: 'East', className: 'resize-handle-e', label: 'Redimensionar pela direita' },
  { direction: 'NorthWest', className: 'resize-handle-nw', label: 'Redimensionar pelo canto superior esquerdo' },
  { direction: 'NorthEast', className: 'resize-handle-ne', label: 'Redimensionar pelo canto superior direito' },
  { direction: 'SouthWest', className: 'resize-handle-sw', label: 'Redimensionar pelo canto inferior esquerdo' },
  { direction: 'SouthEast', className: 'resize-handle-se', label: 'Redimensionar pelo canto inferior direito' },
];

function App() {
  const [coreStatus, setCoreStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [activeTab, setActiveTab] = useState<'chat' | 'devices'>('chat');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [rendered, setRendered] = useState(false);
  const { theme, setTheme } = useTheme();

  // Chat em três colunas (neo-core/docs/desktop-chat-tres-colunas-plan.md): só na
  // aba Chat e só com a janela larga. Abaixo de 960 px a tela é a de sempre.
  const layout = useWideLayout();
  const [leftOpen, setLeftOpen] = useState(() => readColumnOpen('left'));
  const [rightOpen, setRightOpen] = useState(() => readColumnOpen('right'));
  // Na janela média a direita abre POR CIMA do chat e não fica salva: ao mudar de
  // largura ela fecha (ajuste no render, sem efeito — padrão do React).
  const [rightDrawer, setRightDrawer] = useState(false);
  const [drawerLayout, setDrawerLayout] = useState(layout);
  if (drawerLayout !== layout) {
    setDrawerLayout(layout);
    setRightDrawer(false);
  }
  const columns = activeTab === 'chat' && layout !== 'compact';
  const showLeft = columns && leftOpen;
  const showRight = columns && (layout === 'wide' ? rightOpen : rightDrawer);

  const toggleLeft = () => {
    setLeftOpen(!leftOpen);
    storeColumnOpen('left', !leftOpen);
  };
  const setRight = (open: boolean) => {
    if (layout === 'wide') {
      setRightOpen(open);
      storeColumnOpen('right', open);
    } else {
      setRightDrawer(open);
    }
  };

  useEffect(() => {
    if (!rightDrawer) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setRightDrawer(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [rightDrawer]);

  const handleWindowDragStart = (event: React.MouseEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    WindowService.startDragging();
  };

  const handleResizeStart = (direction: WindowResizeDirection) => (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    WindowService.startResizeDragging(direction);
  };

  console.log('[Neo] App rendering, mode:', _urlMode);

  useEffect(() => {
    console.log('[Neo] App mounted');
    setRendered(true);
    let cancelled = false;

    // Tetos do navegador headless: o Rust não persiste, então o que o usuário
    // escolheu na Config é reaplicado aqui, uma vez, no boot.
    void applySaved();

    const checkCoreStatus = async () => {
      try {
        console.log('[Neo] Checking core status via window.electronAPI');
        const api = window.electronAPI;
        console.log('[Neo] electronAPI available:', !!api);
        const result = await window.electronAPI?.getNeoCoreStatus?.();
        console.log('[Neo] Core status result:', result);
        if (cancelled) return;

        setCoreStatus(result?.connected ? 'connected' : 'disconnected');
      } catch (err) {
        console.error('[Neo] Core status error:', err);
        if (!cancelled) setCoreStatus('disconnected');
      }
    };

    checkCoreStatus();
    const interval = setInterval(checkCoreStatus, 8000);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    window.electronAPI?.setNotificationContext(activeTab).catch((err) => {
      console.warn('[Neo] Failed to sync notification context:', err);
    });
  }, [activeTab]);

  if (_urlMode === 'quick-help') {
    document.documentElement.style.background = 'transparent';
    document.body.style.background = 'transparent';
    return <QuickHelpPage />;
  }

  return (
    <div className="app">
      {/* Floating orbs for glassmorphism background */}
      <div className="bg-orbs">
        <div className="orb orb-1"></div>
        <div className="orb orb-2"></div>
        <div className="orb orb-3"></div>
        <div className="orb orb-4"></div>
        <div className="orb orb-5"></div>
      </div>

      {RESIZE_HANDLES.map((handle) => (
        <div
          key={handle.direction}
          className={`resize-handle ${handle.className}`}
          role="presentation"
          aria-label={handle.label}
          onMouseDown={handleResizeStart(handle.direction)}
        />
      ))}

      <div className="window-titlebar" onMouseDown={handleWindowDragStart}>
        <div className="window-controls" aria-label="Controles da janela" onMouseDown={(event) => event.stopPropagation()}>
          <button
            type="button"
            className="window-control window-control-close"
            onClick={() => WindowService.close()}
            aria-label="Fechar"
          />
          <button
            type="button"
            className="window-control window-control-minimize"
            onClick={() => WindowService.minimize()}
            aria-label="Minimizar"
          />
          <button
            type="button"
            className="window-control window-control-maximize"
            onClick={() => WindowService.toggleMaximize()}
            aria-label="Maximizar ou restaurar"
          />
        </div>
        <div className="window-title">Neo Assistant</div>
        <div className="window-titlebar-spacer" />
      </div>

      <div className="titlebar">
        <div className="titlebar-drag" onMouseDown={handleWindowDragStart}>
          <div className="header-left">
            <div className="header-avatar">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="11" width="18" height="10" rx="2" />
                <circle cx="9" cy="16" r="1" fill="currentColor" />
                <circle cx="15" cy="16" r="1" fill="currentColor" />
                <path d="M8 11V7a4 4 0 0 1 8 0v4" />
              </svg>
            </div>
            <div className="header-info">
              <span className="header-name">Neo Assistant</span>
              <span className={`header-status ${coreStatus}`}>
                <span className="status-indicator"></span>
                {coreStatus === 'connected'
                  ? 'Conectado'
                  : coreStatus === 'checking'
                    ? 'Verificando conexão...'
                    : 'Desconectado'}
              </span>
            </div>
          </div>
        </div>
        <div className="titlebar-tabs">
          <button
            className={`tab-btn ${activeTab === 'chat' ? 'active' : ''}`}
            onClick={() => setActiveTab('chat')}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            Chat
          </button>
          <button
            className={`tab-btn ${activeTab === 'devices' ? 'active' : ''}`}
            onClick={() => setActiveTab('devices')}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
              <line x1="8" y1="21" x2="16" y2="21"/>
              <line x1="12" y1="17" x2="12" y2="21"/>
            </svg>
            Devices
          </button>
        </div>
        <div className="titlebar-buttons">
          {/* Com a coluna de conversas na tela, "Novo chat" e a lista estão nela. */}
          {!showLeft && (
            <button
              className="btn-new-chat-header"
              onClick={() => window.dispatchEvent(new CustomEvent('neo-new-chat'))}
              title="Novo chat"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              Novo chat
            </button>
          )}
          {!showLeft && (
            <button
              className="btn-sessions-header"
              title="Sessões anteriores"
              onClick={() => setSessionsOpen(!sessionsOpen)}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
                <polyline points="3 3 3 8 8 8" />
                <polyline points="12 7 12 12 15.5 14" />
              </svg>
            </button>
          )}
          {columns && (
            <>
              <button
                type="button"
                className={`btn-column-toggle${showLeft ? ' active' : ''}`}
                onClick={toggleLeft}
                aria-pressed={showLeft}
                aria-label={showLeft ? 'Esconder conversas' : 'Mostrar conversas'}
                title={showLeft ? 'Esconder conversas' : 'Mostrar conversas'}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <line x1="9" y1="3" x2="9" y2="21" />
                </svg>
              </button>
              <button
                type="button"
                className={`btn-column-toggle${showRight ? ' active' : ''}`}
                onClick={() => setRight(!showRight)}
                aria-pressed={showRight}
                aria-label={showRight ? 'Esconder segundo plano' : 'Mostrar segundo plano'}
                title={showRight ? 'Esconder segundo plano' : 'Mostrar segundo plano'}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <line x1="15" y1="3" x2="15" y2="21" />
                </svg>
              </button>
              <span className="titlebar-divider" aria-hidden="true" />
            </>
          )}
          <button
            className="btn-settings-header"
            title="Configurações"
            onClick={() => setSettingsOpen(true)}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
      </div>

      {/* As posições dos filhos são fixas (coluna | centro | coluna, com `null`
          no lugar da que está escondida): mostrar ou esconder uma coluna, ou
          mudar a largura da janela, NÃO remonta o ChatPage (perderia o turno). */}
      <div className={`app-content${columns ? ` app-content--cols layout-${layout}` : ''}`}>
        {activeTab === 'chat' ? (
          <>
            {showLeft ? (
              <aside className="side-col side-left" aria-label="Conversas e agentes em ação">
                <ConversationsPanel />
                <AgentActivityPanel />
              </aside>
            ) : null}
            <div className={`chat-center${columns ? ' chat-center--cols' : ''}`}>
              {columns ? <ConversationHeader onShowJobs={() => setRight(true)} /> : null}
              <ChatPage />
            </div>
            {showRight ? (
              <aside
                className={`side-col side-right${layout === 'medium' ? ' side-drawer' : ''}`}
                aria-label="Tarefas em segundo plano"
              >
                <JobsColumn onClose={layout === 'medium' ? () => setRightDrawer(false) : undefined} />
              </aside>
            ) : null}
          </>
        ) : (
          <DevicesTab />
        )}
      </div>

      <SessionsSidebar
        open={sessionsOpen}
        onClose={() => setSessionsOpen(false)}
        onSelectSession={(sessionId) => {
          setSessionsOpen(false);
          window.dispatchEvent(new CustomEvent('neo-load-session', { detail: { sessionId } }));
        }}
      />

      {settingsOpen && (
        <SettingsModal
          theme={theme}
          onThemeChange={setTheme}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </div>
  );
}

export default App;
