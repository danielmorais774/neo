import { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import './ui/neo-ui.css';
import LoginScreen from './LoginScreen';
import {
  LEGACY_PANEL_TOKEN,
  fetchAuthMode,
  isAuthenticated,
  subscribe,
  type AuthMode,
} from './lib/auth';
import ChatScreen from './screens/ChatScreen';
import ComunicacaoScreen from './screens/ComunicacaoScreen';
import MemoriaScreen from './screens/MemoriaScreen';
import VisaoScreen from './screens/VisaoScreen';
import LogsScreen from './screens/LogsScreen';
import DebugPanel from './DebugPanel';
import ResearchPanel from './ResearchPanel';
import AgendaPanel from './AgendaPanel';
import AffectPanel from './AffectPanel';
import SettingsPanel from './SettingsPanel';
import SkillsPanel from './SkillsPanel';
import ServicesPanel from './ServicesPanel';
import WakeTrainingPanel from './WakeTrainingPanel';
import FilesPanel from './FilesPanel';
import SpotifyPanel from './SpotifyPanel';
import SirenePanel from './SirenePanel';
import DadosPanel from './DadosPanel';
import LiberacoesPanel from './LiberacoesPanel';
import Sidebar from './Sidebar';
import { ToastContainer, useToast } from './ToastNotification';

function App() {
  const { toasts, removeToast } = useToast();
  const [logado, setLogado] = useState(isAuthenticated);
  const [modo, setModo] = useState<AuthMode | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);

  // A sessão pode cair no meio do uso (token vencido e refresh queimado): o
  // wrapper de fetch limpa o estado e este subscribe traz a tela de login de
  // volta, em vez de deixar o painel mostrando telas vazias sem motivo.
  useEffect(() => subscribe(() => setLogado(isAuthenticated())), []);

  // **Sem token, tela de login** — independente do degrau do portão.
  //
  // A primeira versão disto perguntava ao core se login era obrigatório
  // (`login_required`, ou seja, `enforce`/`strict`) e, em `shadow`, deixava o
  // painel entrar direto. Estava errado: o token estático saiu do fonte, então
  // "entrar direto" significava entrar SEM CREDENCIAL NENHUMA — e as telas de
  // administração passam por `require_panel_jwt`, que recusa a ausência de token
  // em qualquer modo. O painel abria e cada tela morria em 401, sem dizer por quê.
  //
  // `LEGACY_PANEL_TOKEN` (VITE_PANEL_TOKEN) é a única dispensa: quem passou um
  // token por ambiente já tem credencial.
  const precisaLogin = !logado && !LEGACY_PANEL_TOKEN;

  // O modo não decide se a tela aparece; serve para EXPLICAR a situação na tela
  // (e para avisar, no `strict`, que o token antigo morreu de vez).
  useEffect(() => {
    let vivo = true;
    fetchAuthMode()
      .then((m) => vivo && setModo(m))
      .catch(() => vivo && setModo(null));
    return () => {
      vivo = false;
    };
  }, [logado]);

  useEffect(() => {
    if (!precisaLogin) {
      setAviso(null);
    } else if (modo?.mode === 'strict') {
      setAviso('Este core está em modo strict: só login de usuário é aceito.');
    } else if (modo && !modo.login_required) {
      setAviso(
        `O portão está em "${modo.mode}", mas as telas de administração pedem ` +
          'credencial de qualquer jeito. Entre com seu usuário.',
      );
    } else {
      setAviso(null);
    }
  }, [precisaLogin, modo]);

  if (precisaLogin) {
    return <LoginScreen aviso={aviso} onEntrou={() => setLogado(true)} />;
  }

  return (
    <BrowserRouter>
      <div className="neo-app">
        <Sidebar />
        <main className="neo-main">
          <Routes>
            <Route path="/" element={<Navigate to="/comunicacao" replace />} />

            <Route path="/chat" element={<ChatScreen />} />
            <Route path="/comunicacao" element={<ComunicacaoScreen />} />
            <Route path="/musica" element={<SpotifyPanel />} />
            <Route path="/pesquisas" element={<ResearchPanel />} />
            <Route path="/agenda" element={<AgendaPanel />} />
            <Route path="/servicos" element={<ServicesPanel />} />
            <Route path="/liberacoes" element={<LiberacoesPanel />} />
            <Route path="/arquivos" element={<FilesPanel />} />
            {/* Cadastros generativos: a MESMA tela serve todos os cadastros —
                o :slug escolhe qual schema renderizar. */}
            <Route path="/dados" element={<DadosPanel />} />
            <Route path="/dados/:slug" element={<DadosPanel />} />
            <Route path="/sirene" element={<SirenePanel />} />

            <Route path="/memoria" element={<MemoriaScreen />} />
            <Route path="/emocoes" element={<AffectPanel />} />
            <Route path="/skills" element={<SkillsPanel />} />

            <Route path="/visao" element={<VisaoScreen />} />

            <Route path="/sessoes" element={<DebugPanel />} />
            <Route path="/logs" element={<LogsScreen />} />

            <Route path="/wake" element={<WakeTrainingPanel />} />

            <Route path="/config" element={<SettingsPanel />} />

            {/*
              Rotas antigas: o painel vive aberto num pin do navegador, então os
              caminhos velhos continuam resolvendo — cada um cai na aba certa da
              tela que o absorveu, em vez de dar 404 mudo.
            */}
            <Route path="/whatsapp" element={<Navigate to="/comunicacao?tab=conversas" replace />} />
            <Route path="/missions" element={<Navigate to="/comunicacao?tab=missoes" replace />} />
            <Route path="/whatsapp-status" element={<Navigate to="/config?tab=comunicacoes" replace />} />
            <Route path="/devices" element={<Navigate to="/config?tab=dispositivos" replace />} />
            <Route path="/settings" element={<Navigate to="/config" replace />} />
            <Route path="/debug" element={<Navigate to="/sessoes" replace />} />
            <Route path="/session-logs" element={<Navigate to="/logs?tab=sessoes" replace />} />
            <Route path="/vision" element={<Navigate to="/visao?tab=video" replace />} />
            <Route path="/frame" element={<Navigate to="/visao?tab=frame" replace />} />
            <Route path="/pks" element={<Navigate to="/memoria?tab=pks" replace />} />
            <Route path="/knowledge-graph" element={<Navigate to="/memoria?tab=grafo" replace />} />
            <Route path="/pks-context" element={<Navigate to="/memoria?tab=contexto" replace />} />
            <Route path="/research" element={<Navigate to="/pesquisas" replace />} />
            <Route path="/affect" element={<Navigate to="/emocoes" replace />} />
            <Route path="/spotify" element={<Navigate to="/musica" replace />} />

            <Route path="*" element={<Navigate to="/comunicacao" replace />} />
          </Routes>
        </main>
        <ToastContainer toasts={toasts} onRemove={removeToast} position="bottom-right" />
      </div>
    </BrowserRouter>
  );
}

export default App;
