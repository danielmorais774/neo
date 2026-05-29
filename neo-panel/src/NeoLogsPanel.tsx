import { useEffect, useState } from 'react';

const NeoLogsPanel = () => {
  const [logContent, setLogContent] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [tail, setTail] = useState(500);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchLogs();
  }, []);

  const fetchLogs = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`http://192.168.4.175:8000/api/v1/app-logs?tail=${tail}`);
      if (response.ok) {
        const content = await response.text();
        setLogContent(content);
      } else {
        setError(`HTTP ${response.status}`);
      }
    } catch (e) {
      setError('Failed to connect to neo-core');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '12px 16px',
        borderBottom: '1px solid #1e2235',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 12,
        flexShrink: 0,
      }}>
        <span style={{ color: '#a78bfa', fontWeight: 600, fontSize: 14 }}>
          Logs (neo-core)
        </span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <select
            value={tail}
            onChange={(e) => setTail(Number(e.target.value))}
            style={{
              background: '#1e2235',
              border: '1px solid #2d3348',
              borderRadius: 4,
              padding: '4px 8px',
              color: '#e2e8f0',
              fontSize: 12,
            }}
          >
            <option value={100}>100 linhas</option>
            <option value={500}>500 linhas</option>
            <option value={1000}>1000 linhas</option>
            <option value={2000}>2000 linhas</option>
            <option value={5000}>5000 linhas</option>
          </select>
          <button
            onClick={fetchLogs}
            style={{
              background: '#7c3aed',
              border: 'none',
              borderRadius: 4,
              padding: '4px 12px',
              color: 'white',
              fontSize: 12,
              cursor: 'pointer',
            }}
          >
            Atualizar
          </button>
        </div>
      </div>

      {/* Log content */}
      <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
        {loading ? (
          <div style={{ color: '#64748b' }}>Carregando logs...</div>
        ) : error ? (
          <div style={{ color: '#ef4444' }}>Erro: {error}</div>
        ) : (
          <pre style={{
            color: '#e2e8f0',
            fontSize: 12,
            fontFamily: 'monospace',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            margin: 0,
            lineHeight: 1.6,
          }}>
            {logContent}
          </pre>
        )}
      </div>
    </div>
  );
};

export default NeoLogsPanel;