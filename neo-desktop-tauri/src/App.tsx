import { useEffect, useState } from 'react';
import { ChatPage, QuickHelpPage } from './pages';
import { WindowService } from './lib';
import { DevicesTab } from './components/DevicesTab';
import './App.css';
import './index.css';

const _urlMode = new URLSearchParams(window.location.search).get('mode');

function App() {
  const [coreStatus, setCoreStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [activeTab, setActiveTab] = useState<'chat' | 'devices'>('chat');
  const [rendered, setRendered] = useState(false);

  console.log('[Neo] App rendering, mode:', _urlMode);

  useEffect(() => {
    console.log('[Neo] App mounted');
    setRendered(true);
    let cancelled = false;

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

      <div className="titlebar">
        <div className="titlebar-drag">
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
            Dispositivos
          </button>
        </div>
        <div className="titlebar-buttons">
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
          <button className="btn-settings-header" title="Configurações">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
          <button onClick={() => WindowService.minimize()} className="btn-minimize">
            −
          </button>
          <button onClick={() => WindowService.close()} className="btn-close">
            ×
          </button>
        </div>
      </div>

      <div className="app-content">
        {activeTab === 'chat' ? <ChatPage /> : <DevicesTab />}
      </div>
    </div>
  );
}

export default App;
