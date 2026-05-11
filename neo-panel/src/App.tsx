import { Admin, Resource } from 'react-admin';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import DebugPanel from './DebugPanel';
import NeoFrameAnalyzer from './NeoFrameAnalyzer';
import NeoVisionPanel from './NeoVisionPanel';
import SessionLogsPanel from './SessionLogsPanel';
import KnowledgeGraphPanel from './KnowledgeGraphPanel';
import WhatsAppPanel from './WhatsAppPanel';
import WhatsAppStatusPanel from './WhatsAppStatusPanel';
import MissionsPanel from './MissionsPanel';
import ConnectedDevicesPanel from './ConnectedDevicesPanel';
import Sidebar from './Sidebar';
import { ToastContainer, useToast } from './ToastNotification';

const dataProvider = {
  getList: async (resource: string, _params: any) => {
    const response = await fetch(`http://localhost:8000/logs/${resource}`);
    if (!response.ok) {
      throw new Error('Failed to fetch logs');
    }
    const data = await response.json();
    return {
      data: data.logs.map((log: any, index: number) => ({ ...log, id: `${resource}-${index}` })),
      total: data.logs.length,
    };
  },
  getOne: () => Promise.reject(new Error('Not implemented')),
  getMany: () => Promise.reject(new Error('Not implemented')),
  getManyReference: () => Promise.reject(new Error('Not implemented')),
  create: () => Promise.reject(new Error('Not implemented')),
  update: () => Promise.reject(new Error('Not implemented')),
  updateMany: () => Promise.reject(new Error('Not implemented')),
  delete: () => Promise.reject(new Error('Not implemented')),
  deleteMany: () => Promise.reject(new Error('Not implemented')),
};

const stages = ['validation', 'classification', 'task_analysis', 'external', 'build_prompt', 'generate_response', 'post_process'];

const AllLogsList = () => {
  const [logsByStage, setLogsByStage] = useState<Record<string, any[]>>({});

  const fetchAllLogs = async () => {
    const logsPromises = stages.map(async (stage) => {
      try {
        const response = await fetch(`http://localhost:8000/logs/${stage}`);
        if (!response.ok) return { stage, logs: [] };
        const data = await response.json();
        return { stage, logs: data.logs.map((log: any, index: number) => ({ ...log, id: `${stage}-${index}` })) };
      } catch {
        return { stage, logs: [] };
      }
    });
    const results = await Promise.all(logsPromises);
    const logsObj: Record<string, any[]> = {};
    results.forEach(({ stage, logs }) => {
      logsObj[stage] = logs.sort((a: any, b: any) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    });
    setLogsByStage(logsObj);
  };

  useEffect(() => {
    fetchAllLogs();
    const interval = setInterval(fetchAllLogs, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      <h1>All Pipeline Logs</h1>
      <button onClick={async () => {
        await fetch('http://localhost:8000/clear-logs', { method: 'POST' });
        fetchAllLogs();
      }} style={{ marginBottom: '20px' }}>Clear Logs</button>
      {stages.map((stage) => (
        <div key={stage} style={{ marginBottom: '20px' }}>
          <h2>{stage}</h2>
          <textarea
            readOnly
            style={{ width: '300%', height: '400px' }}
            value={logsByStage[stage]?.map(log => `${new Date(log.timestamp).toLocaleString()}: \n${stage === 'build_prompt' ? log.data.prompt : JSON.stringify(log.data)}`).join('\n') || ''}
          />
        </div>
      ))}
    </div>
  );
};

const Dashboard = () => <AllLogsList />;

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
        overflow: 'hidden'
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
              <Route path="/logs" element={
                <Admin dataProvider={dataProvider} dashboard={Dashboard}>
                  <Resource name="validation" />
                  <Resource name="classification" />
                  <Resource name="external" />
                  <Resource name="build_prompt" />
                  <Resource name="generate_response" />
                  <Resource name="post_process" />
                </Admin>
              } />
            </Routes>
          </main>
        </div>
        <ToastContainer toasts={toasts} onRemove={removeToast} position="bottom-right" />
      </div>
    </BrowserRouter>
  );
}

export default App;