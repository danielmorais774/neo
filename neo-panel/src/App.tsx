import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import DebugPanel from './DebugPanel';
import NeoFrameAnalyzer from './NeoFrameAnalyzer';
import NeoVisionPanel from './NeoVisionPanel';
import SessionLogsPanel from './SessionLogsPanel';
import KnowledgeGraphPanel from './KnowledgeGraphPanel';
import WhatsAppPanel from './WhatsAppPanel';
import WhatsAppStatusPanel from './WhatsAppStatusPanel';
import MissionsPanel from './MissionsPanel';
import ConnectedDevicesPanel from './ConnectedDevicesPanel';
import NeoLogsPanel from './NeoLogsPanel';
import LettaPanel from './LettaPanel';
import PKSPanel from './PKSPanel';
import PKSContextDebugPanel from './PKSContextDebugPanel';
import SettingsPanel from './SettingsPanel';
import Sidebar from './Sidebar';
import { ToastContainer, useToast } from './ToastNotification';

function App() {
  const { toasts, removeToast } = useToast();

  return (
    <BrowserRouter>
      <div style={{
        background: '#0f1117',
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        width: '100%',
        overflow: 'auto'
      }}>
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
          <Sidebar />
          <main style={{ flex: 1, overflow: 'auto' }}>
            <Routes>
              <Route path="/" element={<Navigate to="/whatsapp-status" replace />} />
              <Route path="/whatsapp-status" element={<WhatsAppStatusPanel />} />
              <Route path="/whatsapp" element={<WhatsAppPanel />} />
              <Route path="/debug" element={<DebugPanel />} />
              <Route path="/session-logs" element={<SessionLogsPanel />} />
              <Route path="/vision" element={<NeoVisionPanel />} />
              <Route path="/frame" element={<NeoFrameAnalyzer />} />
              <Route path="/knowledge-graph" element={<KnowledgeGraphPanel />} />
              <Route path="/missions" element={<MissionsPanel />} />
              <Route path="/devices" element={<ConnectedDevicesPanel />} />
              <Route path="/logs" element={<NeoLogsPanel />} />
              <Route path="/letta" element={<LettaPanel />} />
              <Route path="/pks" element={<PKSPanel />} />
              <Route path="/pks-context" element={<PKSContextDebugPanel />} />
              <Route path="/settings" element={<SettingsPanel />} />
            </Routes>
          </main>
        </div>
        <ToastContainer toasts={toasts} onRemove={removeToast} position="bottom-right" />
      </div>
    </BrowserRouter>
  );
}

export default App;