import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import './ui/neo-ui.css';
import ChatScreen from './screens/ChatScreen';
import ComunicacaoScreen from './screens/ComunicacaoScreen';
import MemoriaScreen from './screens/MemoriaScreen';
import VisaoScreen from './screens/VisaoScreen';
import LogsScreen from './screens/LogsScreen';
import DebugPanel from './DebugPanel';
import ResearchPanel from './ResearchPanel';
import AffectPanel from './AffectPanel';
import SettingsPanel from './SettingsPanel';
import SkillsPanel from './SkillsPanel';
import ServicesPanel from './ServicesPanel';
import WakeTrainingPanel from './WakeTrainingPanel';
import FilesPanel from './FilesPanel';
import SpotifyPanel from './SpotifyPanel';
import SirenePanel from './SirenePanel';
import LettaPanel from './LettaPanel';
import DadosPanel from './DadosPanel';
import Sidebar from './Sidebar';
import { ToastContainer, useToast } from './ToastNotification';

function App() {
  const { toasts, removeToast } = useToast();

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
            <Route path="/servicos" element={<ServicesPanel />} />
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

            {/*
              Letta saiu da sidebar (runtime descartado na decisão da Memória
              Semântica v3). A rota fica de pé pra não apagar acesso a algo que
              ainda responde — se confirmar que morreu, some com o painel todo.
            */}
            <Route path="/letta" element={<LettaPanel />} />

            <Route path="*" element={<Navigate to="/comunicacao" replace />} />
          </Routes>
        </main>
        <ToastContainer toasts={toasts} onRemove={removeToast} position="bottom-right" />
      </div>
    </BrowserRouter>
  );
}

export default App;
