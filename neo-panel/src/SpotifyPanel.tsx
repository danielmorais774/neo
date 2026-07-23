import { useEffect, useState, useCallback } from 'react';

// Spotify connect panel (neo-music-feature). Connects a user's Spotify account so
// neo-core can search + control playback via the Connect API. The OAuth routes are
// open (callback is a browser redirect) and bound by a signed state — see
// neo-core/api/routes/spotify.py.
const SPOTIFY_API = '/api/v1/spotify';
const USERS_API = '/api/v1/knowledge-graph/users';

interface Status {
  configured: boolean;
  connected: boolean;
}

export default function SpotifyPanel() {
  const [users, setUsers] = useState<string[]>([]);
  const [userId, setUserId] = useState('');
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the known users (default to the first).
  useEffect(() => {
    fetch(USERS_API)
      .then((r) => (r.ok ? r.json() : { user_ids: [] }))
      .then((d: { user_ids: string[] }) => {
        setUsers(d.user_ids ?? []);
        if (d.user_ids?.length > 0) setUserId(d.user_ids[0]);
      })
      .catch(() => {});
  }, []);

  const refreshStatus = useCallback(async () => {
    if (!userId.trim()) {
      setStatus(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${SPOTIFY_API}/status?user_id=${encodeURIComponent(userId.trim())}`);
      if (!res.ok) throw new Error(`status ${res.status}`);
      setStatus(await res.json());
    } catch (e) {
      setError(`Falha ao consultar status: ${e}`);
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  // Re-check after the OAuth popup is closed / the window regains focus.
  useEffect(() => {
    const onFocus = () => refreshStatus();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [refreshStatus]);

  const connect = () => {
    if (!userId.trim()) return;
    window.open(
      `${SPOTIFY_API}/authorize?user_id=${encodeURIComponent(userId.trim())}`,
      'spotify-connect',
      'width=520,height=720',
    );
  };

  const disconnect = async () => {
    if (!userId.trim()) return;
    setLoading(true);
    try {
      await fetch(`${SPOTIFY_API}/disconnect?user_id=${encodeURIComponent(userId.trim())}`, {
        method: 'POST',
      });
      await refreshStatus();
    } catch (e) {
      setError(`Falha ao desconectar: ${e}`);
    } finally {
      setLoading(false);
    }
  };

  const dot = (on: boolean) => ({
    display: 'inline-block',
    width: 10,
    height: 10,
    borderRadius: '50%',
    marginRight: 8,
    background: on ? '#1db954' : '#666',
  });

  return (
    <div style={{ padding: 24, color: '#eee', maxWidth: 640 }}>
      <h2 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>🎵 Música</h2>
      <p style={{ color: '#aaa', marginTop: 4 }}>
        Conecte o Spotify para o Neo buscar e tocar músicas (busca + controle do device ativo).
      </p>

      <div
        style={{
          background: '#1a1a1a',
          border: '1px solid #2a2a2a',
          borderRadius: 12,
          padding: 20,
          marginTop: 16,
        }}
      >
        <label style={{ fontSize: 13, color: '#aaa' }}>Usuário</label>
        <div style={{ display: 'flex', gap: 8, marginTop: 6, marginBottom: 18 }}>
          {users.length > 0 ? (
            <select
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              style={{
                flex: 1,
                background: '#111',
                color: '#eee',
                border: '1px solid #333',
                borderRadius: 8,
                padding: '8px 10px',
              }}
            >
              {users.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              placeholder="user_id (UUID)"
              style={{
                flex: 1,
                background: '#111',
                color: '#eee',
                border: '1px solid #333',
                borderRadius: 8,
                padding: '8px 10px',
              }}
            />
          )}
          <button
            onClick={refreshStatus}
            disabled={loading || !userId.trim()}
            style={{
              background: '#222',
              color: '#eee',
              border: '1px solid #333',
              borderRadius: 8,
              padding: '8px 14px',
              cursor: 'pointer',
            }}
          >
            ↻
          </button>
        </div>

        <div style={{ marginBottom: 6 }}>
          <span style={dot(!!status?.configured)} />
          App configurado (client_id/secret){' '}
          {status && !status.configured && (
            <span style={{ color: '#e0a020' }}>— defina SPOTIFY_CLIENT_ID/SECRET no neo-core</span>
          )}
        </div>
        <div style={{ marginBottom: 18 }}>
          <span style={dot(!!status?.connected)} />
          {status?.connected ? 'Conta conectada' : 'Conta não conectada'}
        </div>

        {status?.connected ? (
          <button
            onClick={disconnect}
            disabled={loading}
            style={{
              background: '#3a1a1a',
              color: '#ff8080',
              border: '1px solid #5a2a2a',
              borderRadius: 8,
              padding: '10px 18px',
              cursor: 'pointer',
            }}
          >
            Desconectar
          </button>
        ) : (
          <button
            onClick={connect}
            disabled={loading || !status?.configured || !userId.trim()}
            style={{
              background: '#1db954',
              color: '#04210f',
              border: 'none',
              borderRadius: 8,
              padding: '10px 18px',
              fontWeight: 700,
              cursor: status?.configured ? 'pointer' : 'not-allowed',
              opacity: status?.configured ? 1 : 0.5,
            }}
          >
            Conectar Spotify
          </button>
        )}

        {error && <div style={{ color: '#ff8080', marginTop: 12 }}>{error}</div>}
      </div>

      <p style={{ color: '#777', fontSize: 13, marginTop: 16 }}>
        YouTube não precisa de login — só da <code>YOUTUBE_API_KEY</code> no neo-core. O Neo decide
        Spotify vs YouTube pelo seu pedido e pelo que está aberto.
      </p>
    </div>
  );
}
