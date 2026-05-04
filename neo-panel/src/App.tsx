import { Admin, Resource } from 'react-admin';
import { useEffect, useState } from 'react';
import DebugPanel from './DebugPanel';
import NeoFrameAnalyzer from './NeoFrameAnalyzer';
import NeoVisionPanel from './NeoVisionPanel';
import SessionLogsPanel from './SessionLogsPanel';
import KnowledgeGraphPanel from './KnowledgeGraphPanel';
import WhatsAppPanel from './WhatsAppPanel';
import WhatsAppStatusPanel from './WhatsAppStatusPanel';

const dataProvider = {
  getList: async (resource: string, _params: any) => {
    const response = await fetch(`http://localhost:8001/logs/${resource}`);
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
        const response = await fetch(`http://localhost:8001/logs/${stage}`);
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
        await fetch('http://localhost:8001/clear-logs', { method: 'POST' });
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

const TAB_STYLE = (active: boolean): React.CSSProperties => ({
  padding: '8px 20px',
  cursor: 'pointer',
  border: 'none',
  borderBottom: active ? '2px solid #7c3aed' : '2px solid transparent',
  background: 'transparent',
  color: active ? '#a78bfa' : '#64748b',
  fontWeight: active ? 600 : 400,
  fontSize: 14,
  transition: 'color 0.15s',
})

function App() {
  const [tab, setTab] = useState<'logs' | 'debug' | 'vision' | 'frame' | 'session-logs' | 'knowledge-graph' | 'whatsapp' | 'whatsapp-status'>('whatsapp-status')

  return (
    <div style={{ background: '#0f1117', height: '100vh', display: 'flex', flexDirection: 'column', width: '100%', overflow: 'hidden' }}>
      {/* Top tab bar */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        background: '#13161f',
        borderBottom: '1px solid #1e2235',
        padding: '0 20px',
      }}>
        <span style={{ color: '#7c3aed', fontWeight: 700, marginRight: 12 }}>Neo Panel</span>
        <button style={TAB_STYLE(tab === 'debug')} onClick={() => setTab('debug')}>🔬 Debug</button>
        <button style={TAB_STYLE(tab === 'session-logs')} onClick={() => setTab('session-logs')}>📝 Session Logs</button>
        <button style={TAB_STYLE(tab === 'vision')} onClick={() => setTab('vision')}>🎬 Vision</button>
        <button style={TAB_STYLE(tab === 'frame')} onClick={() => setTab('frame')}>🔍 Frame</button>
        <button style={TAB_STYLE(tab === 'knowledge-graph')} onClick={() => setTab('knowledge-graph')}>🕸️ Knowledge Graph</button>
        <button style={TAB_STYLE(tab === 'whatsapp')} onClick={() => setTab('whatsapp')}>📱 WhatsApp</button>
        <button style={TAB_STYLE(tab === 'whatsapp-status')} onClick={() => setTab('whatsapp-status')}>🔑 WhatsApp Status</button>
        <button style={TAB_STYLE(tab === 'logs')} onClick={() => setTab('logs')}>📋 Logs (legado)</button>
      </div>

      {tab === 'knowledge-graph' && <KnowledgeGraphPanel />}
      {tab === 'whatsapp' && <WhatsAppPanel />}
      {tab === 'whatsapp-status' && <WhatsAppStatusPanel />}
      {tab === 'debug' && <DebugPanel />}
      {tab === 'session-logs' && <SessionLogsPanel />}
      {tab === 'vision' && <NeoVisionPanel />}
      {tab === 'frame' && <NeoFrameAnalyzer />}
      {tab === 'logs' && (
        <Admin dataProvider={dataProvider} dashboard={Dashboard}>
          <Resource name="validation" />
          <Resource name="classification" />
          <Resource name="external" />
          <Resource name="build_prompt" />
          <Resource name="generate_response" />
          <Resource name="post_process" />
        </Admin>
      )}
    </div>
  )
}

export default App;
