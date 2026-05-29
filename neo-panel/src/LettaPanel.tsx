import { useEffect, useState } from 'react';

interface Agent {
  id: string;
  name: string;
  description?: string;
  created_at: string;
  last_activity_at: string;
  status?: string;
}

interface Block {
  id: string;
  label: string;
  value: string;
  template_type?: string;
}

interface Source {
  id: string;
  name: string;
  description?: string;
  created_at: string;
}

const LETTA_API = '/api/v1/letta';

export default function LettaPanel() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [health, setHealth] = useState<{ version: string; status: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [agentMemory, setAgentMemory] = useState<any>(null);
  const [activeTab, setActiveTab] = useState<'agents' | 'blocks' | 'sources'>('agents');
  const [showAgentForm, setShowAgentForm] = useState(false);
  const [newAgentName, setNewAgentName] = useState('');
  const [newAgentDescription, setNewAgentDescription] = useState('');
  const [newAgentModel, setNewAgentModel] = useState('openai/gpt-4');
  const [newAgentEmbeddingModel, setNewAgentEmbeddingModel] = useState('openai/text-embedding-3-small');
  const [newAgentPersona, setNewAgentPersona] = useState('You are a helpful AI assistant.');
  const [newAgentHuman, setNewAgentHuman] = useState('The user is interacting with you.');
  const [creatingAgent, setCreatingAgent] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const inputStyle: React.CSSProperties = {
    width: '100%',
    padding: '10px 12px',
    background: '#0f172a',
    border: '1px solid #333',
    borderRadius: '6px',
    color: '#fff',
    fontSize: '14px',
    boxSizing: 'border-box'
  };

  const fetchData = async () => {
    try {
      const [agentsRes, blocksRes, sourcesRes, healthRes] = await Promise.all([
        fetch(`${LETTA_API}/v1/agents/`),
        fetch(`${LETTA_API}/v1/blocks/`),
        fetch(`${LETTA_API}/v1/sources/`),
        fetch(`${LETTA_API}/v1/health/`),
      ]);

      if (!agentsRes.ok || !blocksRes.ok || !sourcesRes.ok || !healthRes.ok) {
        throw new Error('Failed to fetch data from Letta');
      }

      const agentsData = await agentsRes.json();
      const blocksData = await blocksRes.json();
      const sourcesData = await sourcesRes.json();
      const healthData = await healthRes.json();

      setAgents(Array.isArray(agentsData) ? agentsData : agentsData.agents || []);
      setBlocks(blocksData.blocks || []);
      setSources(sourcesData.sources || []);
      setHealth(healthData);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  const fetchAgentMemory = async (agentId: string) => {
    try {
      const res = await fetch(`${LETTA_API}/v1/agents/${agentId}/core-memory`);
      if (res.ok) {
        const data = await res.json();
        setAgentMemory(data);
      }
    } catch (err) {
      console.error('Failed to fetch agent memory:', err);
    }
  };

  const createAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAgentName.trim()) return;

    setCreatingAgent(true);
    setFormError(null);

    try {
      const res = await fetch(`${LETTA_API}/v1/agents/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newAgentName.trim(),
          description: newAgentDescription.trim() || undefined,
          model: newAgentModel,
          embedding_model: newAgentEmbeddingModel,
          persona: newAgentPersona,
          human: newAgentHuman,
        }),
      });

      if (!res.ok) {
        const error = await res.json();
        throw new Error(error.detail || 'Failed to create agent');
      }

      setNewAgentName('');
      setNewAgentDescription('');
      setShowAgentForm(false);
      fetchData();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setCreatingAgent(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  useEffect(() => {
    if (selectedAgent) {
      fetchAgentMemory(selectedAgent);
    } else {
      setAgentMemory(null);
    }
  }, [selectedAgent]);

  if (loading) {
    return (
      <div style={{ padding: '20px', color: '#fff' }}>
        <div style={{ fontSize: '18px' }}>Loading Letta data...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: '20px', color: '#ff6b6b' }}>
        <div style={{ fontSize: '18px', marginBottom: '10px' }}>Error: {error}</div>
        <button onClick={fetchData} style={{ padding: '8px 16px', cursor: 'pointer' }}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div style={{ padding: '20px', color: '#fff', height: '100%', overflow: 'auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
        <h1 style={{ margin: 0, fontSize: '24px' }}>Letta Integration</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          {health && (
            <span style={{
              background: health.status === 'ok' ? '#10b981' : '#f59e0b',
              padding: '4px 12px',
              borderRadius: '12px',
              fontSize: '12px'
            }}>
              v{health.version} - {health.status}
            </span>
          )}
          <button onClick={() => setShowAgentForm(true)} style={{
            padding: '8px 16px',
            background: '#10b981',
            border: 'none',
            borderRadius: '6px',
            color: '#fff',
            cursor: 'pointer'
          }}>
            + Create Agent
          </button>
          <button onClick={fetchData} style={{
            padding: '8px 16px',
            background: '#3b82f6',
            border: 'none',
            borderRadius: '6px',
            color: '#fff',
            cursor: 'pointer'
          }}>
            Refresh
          </button>
        </div>
      </div>

      {/* Create Agent Form Modal */}
      {showAgentForm && (
        <div style={{
          position: 'fixed',
          top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.8)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000
        }}>
          <form onSubmit={createAgent} style={{
            background: '#1a1a2e',
            padding: '24px',
            borderRadius: '12px',
            width: '500px',
            maxWidth: '90vw',
            maxHeight: '90vh',
            overflow: 'auto'
          }}>
            <h2 style={{ margin: '0 0 20px 0', color: '#fff' }}>Create New Agent</h2>

            {formError && (
              <div style={{ color: '#ff6b6b', marginBottom: '12px', fontSize: '13px' }}>{formError}</div>
            )}

            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', marginBottom: '6px', color: '#888', fontSize: '13px' }}>Name *</label>
              <input type="text" value={newAgentName} onChange={(e) => setNewAgentName(e.target.value)} required style={inputStyle} placeholder="Agent name" />
            </div>

            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', marginBottom: '6px', color: '#888', fontSize: '13px' }}>Description</label>
              <textarea value={newAgentDescription} onChange={(e) => setNewAgentDescription(e.target.value)} rows={2} style={{...inputStyle, resize: 'vertical'}} placeholder="Optional description" />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', marginBottom: '6px', color: '#888', fontSize: '13px' }}>Model</label>
                <select value={newAgentModel} onChange={(e) => setNewAgentModel(e.target.value)} style={inputStyle}>
                  <option value="openai/gpt-4">openai/gpt-4</option>
                  <option value="openai/gpt-4o">openai/gpt-4o</option>
                  <option value="openai/gpt-4-turbo">openai/gpt-4-turbo</option>
                  <option value="openai/gpt-3.5-turbo">openai/gpt-3.5-turbo</option>
                  <option value="openai/o1">openai/o1</option>
                  <option value="openai/o1-mini">openai/o1-mini</option>
                  <option value="openai/o1-preview">openai/o1-preview</option>
                </select>
              </div>
              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', marginBottom: '6px', color: '#888', fontSize: '13px' }}>Embedding Model</label>
                <select value={newAgentEmbeddingModel} onChange={(e) => setNewAgentEmbeddingModel(e.target.value)} style={inputStyle}>
                  <option value="openai/text-embedding-3-small">openai/text-embedding-3-small</option>
                  <option value="openai/text-embedding-3-large">openai/text-embedding-3-large</option>
                  <option value="openai/text-embedding-ada-002">openai/text-embedding-ada-002</option>
                </select>
              </div>
            </div>

            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', marginBottom: '6px', color: '#888', fontSize: '13px' }}>Persona (System Prompt)</label>
              <textarea value={newAgentPersona} onChange={(e) => setNewAgentPersona(e.target.value)} rows={4} style={{...inputStyle, resize: 'vertical'}} placeholder="You are a helpful AI assistant." />
            </div>

            <div style={{ marginBottom: '20px' }}>
              <label style={{ display: 'block', marginBottom: '6px', color: '#888', fontSize: '13px' }}>Human (User Prompt)</label>
              <textarea value={newAgentHuman} onChange={(e) => setNewAgentHuman(e.target.value)} rows={3} style={{...inputStyle, resize: 'vertical'}} placeholder="The user is interacting with you." />
            </div>

            <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
              <button
                type="button"
                onClick={() => { setShowAgentForm(false); setFormError(null); }}
                style={{
                  padding: '10px 20px',
                  background: '#374151',
                  border: 'none',
                  borderRadius: '6px',
                  color: '#fff',
                  cursor: 'pointer'
                }}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={creatingAgent}
                style={{
                  padding: '10px 20px',
                  background: creatingAgent ? '#374151' : '#10b981',
                  border: 'none',
                  borderRadius: '6px',
                  color: '#fff',
                  cursor: creatingAgent ? 'not-allowed' : 'pointer',
                  opacity: creatingAgent ? 0.6 : 1
                }}
              >
                {creatingAgent ? 'Creating...' : 'Create Agent'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: 'flex', gap: '4px', marginBottom: '20px', borderBottom: '1px solid #333' }}>
        {(['agents', 'blocks', 'sources'] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              padding: '10px 20px',
              background: activeTab === tab ? '#3b82f6' : 'transparent',
              border: 'none',
              borderBottom: activeTab === tab ? '2px solid #3b82f6' : '2px solid transparent',
              color: '#fff',
              cursor: 'pointer',
              textTransform: 'capitalize',
              fontSize: '14px'
            }}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Agents Tab */}
      {activeTab === 'agents' && (
        <div>
          {agents.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: '#666' }}>
              No agents found. Create one via the Letta API.
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '12px' }}>
              {agents.map((agent) => (
                <div
                  key={agent.id}
                  onClick={() => setSelectedAgent(selectedAgent === agent.id ? null : agent.id)}
                  style={{
                    background: selectedAgent === agent.id ? '#1e3a5f' : '#1a1a2e',
                    padding: '16px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border: selectedAgent === agent.id ? '1px solid #3b82f6' : '1px solid #333'
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <h3 style={{ margin: '0 0 4px 0', fontSize: '16px' }}>{agent.name}</h3>
                      <p style={{ margin: 0, fontSize: '12px', color: '#888' }}>{agent.description || 'No description'}</p>
                    </div>
                    <div style={{ textAlign: 'right', fontSize: '11px', color: '#666' }}>
                      <div>ID: {agent.id.slice(0, 8)}...</div>
                      <div>Created: {new Date(agent.created_at).toLocaleDateString()}</div>
                    </div>
                  </div>

                  {selectedAgent === agent.id && agentMemory && (
                    <div style={{ marginTop: '16px', padding: '12px', background: '#0f172a', borderRadius: '6px' }}>
                      <h4 style={{ margin: '0 0 8px 0', fontSize: '13px', color: '#3b82f6' }}>Core Memory</h4>
                      {agentMemory.blocks && agentMemory.blocks.length > 0 ? (
                        agentMemory.blocks.map((block: any) => (
                          <div key={block.id} style={{ marginBottom: '8px' }}>
                            <div style={{ fontSize: '11px', color: '#888', textTransform: 'uppercase' }}>{block.label}</div>
                            <div style={{ fontSize: '13px', whiteSpace: 'pre-wrap' }}>{block.value}</div>
                          </div>
                        ))
                      ) : (
                        <div style={{ fontSize: '12px', color: '#666' }}>No core memory blocks</div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Blocks Tab */}
      {activeTab === 'blocks' && (
        <div>
          {blocks.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: '#666' }}>
              No memory blocks found.
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '12px' }}>
              {blocks.map((block) => (
                <div
                  key={block.id}
                  style={{
                    background: '#1a1a2e',
                    padding: '16px',
                    borderRadius: '8px',
                    border: '1px solid #333'
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                    <div>
                      <h3 style={{ margin: '0 0 4px 0', fontSize: '16px', textTransform: 'uppercase', color: '#a855f7' }}>
                        {block.label}
                      </h3>
                      <div style={{ fontSize: '13px', color: '#ccc', whiteSpace: 'pre-wrap' }}>{block.value}</div>
                    </div>
                    <div style={{ fontSize: '10px', color: '#666' }}>
                      {block.template_type && <div>Type: {block.template_type}</div>}
                      <div>ID: {block.id.slice(0, 8)}...</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Sources Tab */}
      {activeTab === 'sources' && (
        <div>
          {sources.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: '#666' }}>
              No data sources found.
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '12px' }}>
              {sources.map((source) => (
                <div
                  key={source.id}
                  style={{
                    background: '#1a1a2e',
                    padding: '16px',
                    borderRadius: '8px',
                    border: '1px solid #333'
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                    <div>
                      <h3 style={{ margin: '0 0 4px 0', fontSize: '16px' }}>{source.name}</h3>
                      <p style={{ margin: 0, fontSize: '12px', color: '#888' }}>{source.description || 'No description'}</p>
                    </div>
                    <div style={{ fontSize: '10px', color: '#666' }}>
                      <div>ID: {source.id.slice(0, 8)}...</div>
                      <div>Created: {new Date(source.created_at).toLocaleDateString()}</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}