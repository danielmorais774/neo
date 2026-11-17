import { useCallback, useEffect, useState } from 'react';
import './ProvidersTab.css';

// Aba Transcrição: chave do Groq, modelo de transcrição e "só a cota gratuita".
// Backend: neo-core/api/routes/transcricao_config.py — vale na hora, sem reiniciar
// (neo-core/core/transcricao/config.py). A tool do Neo é a `neo_transcrever`.

interface Balde {
  restante: number;
  limite: number;
  janela_s: number;
}

interface TranscricaoConfig {
  /** A tool está montada no core (precisa do sandbox ligado). */
  available: boolean;
  key: {
    /** De onde vem a chave em uso. */
    source: 'painel' | 'env' | 'nenhuma';
    last4: string;
    /** Há chave gravada aqui (mesmo que não decifre). */
    stored: boolean;
    /** A gravada não decifra: a NEO_CRED_KEY mudou. */
    broken: boolean;
    /** Sem NEO_CRED_KEY não dá para gravar. */
    can_store: boolean;
  };
  so_gratuita: boolean;
  modelo: string;
  modelo_padrao: string;
  cota: Partial<Record<'audio_hora' | 'audio_dia' | 'pedidos_minuto' | 'pedidos_dia', Balde>>;
  limites: {
    audio_s_por_hora: number;
    audio_s_por_dia: number;
    pedidos_por_minuto: number;
    pedidos_por_dia: number;
    arquivo_max_mb: number;
  };
}

interface Modelo {
  id: string;
  dono: string;
  ativo: boolean;
}

const BASE = '/api/v1/transcricao-config';

async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* texto cru */
    }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  return res.json();
}

function duracao(segundos: number): string {
  const total = Math.max(0, Math.round(segundos));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h) return `${h}h${String(m).padStart(2, '0')}`;
  if (m) return `${m} min`;
  return `${total} s`;
}

const TranscricaoTab = ({ reloadToken }: { reloadToken: number }) => {
  const [cfg, setCfg] = useState<TranscricaoConfig | null>(null);
  const [modelos, setModelos] = useState<Modelo[] | null>(null);
  const [modelosErro, setModelosErro] = useState<string | null>(null);
  const [keyInput, setKeyInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const carregarModelos = useCallback(async () => {
    setModelosErro(null);
    try {
      const data = await api<{ modelos: Modelo[] }>('GET', '/modelos');
      setModelos(data.modelos);
    } catch (e) {
      setModelos(null);
      setModelosErro(String(e));
    }
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await api<TranscricaoConfig>('GET', '');
        if (!alive) return;
        setCfg(data);
        setError(null);
        if (data.key.source !== 'nenhuma') void carregarModelos();
      } catch (e) {
        if (alive) setError(String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadToken, carregarModelos]);

  const run = async (fn: () => Promise<TranscricaoConfig>, msg: string, recarregarModelos = false) => {
    setBusy(true);
    setError(null);
    setOk(null);
    try {
      const data = await fn();
      setCfg(data);
      setOk(msg);
      if (recarregarModelos) {
        if (data.key.source === 'nenhuma') setModelos(null);
        else void carregarModelos();
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!cfg) {
    return (
      <div className="neo-content">
        <div className="neo-section">
          <h2 className="neo-h2">Transcrição de vídeo</h2>
          <div className="neo-card">
            <div className="neo-note">{error ? `Não foi possível ler a configuração: ${error}` : 'Lendo configuração...'}</div>
          </div>
        </div>
      </div>
    );
  }

  const origem =
    cfg.key.source === 'painel'
      ? `gravada aqui · …${cfg.key.last4}`
      : cfg.key.source === 'env'
        ? `do .env (GROQ_API_KEY) · …${cfg.key.last4}`
        : 'nenhuma — a tool fica fora do Neo';

  // O modelo salvo pode não estar mais na lista do Groq: ele aparece mesmo assim.
  const opcoes = modelos ? [...modelos] : [];
  if (!opcoes.some((m) => m.id === cfg.modelo)) opcoes.unshift({ id: cfg.modelo, dono: '', ativo: true });

  const linhaCota = (rotulo: string, b: Balde | undefined, audio: boolean) =>
    b ? (
      <div>
        {rotulo}:{' '}
        <span className="neo-meta-value">
          {audio ? `${duracao(b.restante)} de ${duracao(b.limite)}` : `${Math.floor(b.restante)} de ${b.limite}`}
        </span>
      </div>
    ) : null;

  return (
    <div className="neo-content">
      <div className="neo-section">
        <h2 className="neo-h2">Transcrição de vídeo (Groq)</h2>
        <p className="neo-desc">
          Quando você pede para transcrever um vídeo ou áudio, o sandbox do Neo só tira o áudio e o Whisper do Groq
          transcreve. O resultado volta como texto (.txt) e legenda (.srt). A chave fica só no neo-core: o sandbox
          nunca a vê. Tudo aqui vale na hora, sem reiniciar.
        </p>

        <div className="neo-card">
          {!cfg.available && (
            <div className="neo-error">
              A tool não está montada no core (o sandbox está desligado no .env), então o Neo não transcreve mesmo
              com chave.
            </div>
          )}

          <h4 className="prov-h4">Chave do Groq</h4>
          <div className="neo-meta">
            <div>
              Em uso: <span className="neo-meta-value">{origem}</span>
            </div>
          </div>
          {cfg.key.broken && (
            <div className="neo-error">
              A chave gravada aqui não decifra (a NEO_CRED_KEY mudou). Grave de novo ou remova.
            </div>
          )}
          <div className="prov-key-row">
            <input
              className="neo-input"
              type="password"
              autoComplete="off"
              placeholder={cfg.key.stored ? 'nova chave (substitui a gravada)' : 'cole a chave gsk_… aqui'}
              value={keyInput}
              disabled={!cfg.key.can_store || busy}
              onChange={(e) => setKeyInput(e.target.value)}
            />
            <button
              className="neo-btn-primary"
              disabled={!keyInput.trim() || busy || !cfg.key.can_store}
              onClick={() =>
                void run(async () => {
                  const data = await api<TranscricaoConfig>('PUT', '/key', { api_key: keyInput });
                  setKeyInput('');
                  return data;
                }, 'Chave testada no Groq e gravada.', true)
              }
            >
              {busy ? '...' : 'Gravar'}
            </button>
            {cfg.key.stored && (
              <button
                className="neo-btn-ghost"
                disabled={busy}
                onClick={() => {
                  if (window.confirm('Remover a chave gravada aqui? Volta a valer a GROQ_API_KEY do .env, se houver.'))
                    void run(() => api<TranscricaoConfig>('DELETE', '/key'), 'Chave removida.', true);
                }}
              >
                Remover
              </button>
            )}
          </div>
          <div className="neo-note">
            {cfg.key.can_store
              ? 'Antes de gravar, o Neo testa a chave na lista de modelos do Groq (não gasta cota). Ela é guardada cifrada.'
              : 'O cofre está desligado (falta NEO_CRED_KEY no .env): só vale a GROQ_API_KEY do .env.'}
          </div>

          <h4 className="prov-h4">Só a cota gratuita</h4>
          <div className="neo-toggle-row">
            <div className="neo-toggle-label">
              Limite:{' '}
              <span className={cfg.so_gratuita ? 'neo-state-on' : 'neo-state-off'}>
                {cfg.so_gratuita ? 'SÓ O GRATUITO' : 'SEM LIMITE DO NEO'}
              </span>
            </div>
            <button
              className={`neo-toggle ${cfg.so_gratuita ? 'is-on' : 'is-off'}`}
              disabled={busy}
              onClick={() =>
                void run(
                  () => api<TranscricaoConfig>('PUT', '', { so_gratuita: !cfg.so_gratuita }),
                  cfg.so_gratuita ? 'Limite da cota gratuita desligado.' : 'Só a cota gratuita, a partir de agora.',
                )
              }
            >
              {cfg.so_gratuita ? '● LIGADO' : '○ DESLIGADO'}
            </button>
          </div>
          <p className="neo-desc">
            {cfg.so_gratuita
              ? 'O Neo faz a conta antes de cada pedido e para antes de passar do plano gratuito, mesmo que a conta aceite mais. Se a cota estiver ocupada, ele diz quando dá para continuar.'
              : 'O Neo não barra nada: quem limita é a sua conta no Groq. Numa conta paga, o que passar do gratuito é cobrado.'}
          </p>

          <h4 className="prov-h4">Modelo</h4>
          <div className="prov-key-row">
            <div className="neo-select-wrap prov-grow">
              <select
                className="neo-select"
                value={cfg.modelo}
                disabled={busy || cfg.key.source === 'nenhuma'}
                onChange={(e) =>
                  void run(() => api<TranscricaoConfig>('PUT', '', { modelo: e.target.value }), `Modelo: ${e.target.value}.`)
                }
              >
                {opcoes.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                    {m.id === cfg.modelo_padrao ? ' (padrão)' : ''}
                    {!m.ativo ? ' — inativo' : ''}
                  </option>
                ))}
              </select>
              <span className="neo-chevron">▼</span>
            </div>
            <button
              className="neo-btn-ghost"
              disabled={busy || cfg.key.source === 'nenhuma'}
              onClick={() => void carregarModelos()}
            >
              Atualizar lista
            </button>
          </div>
          <div className="neo-note">
            {modelosErro
              ? `Não li a lista do Groq: ${modelosErro}`
              : 'A lista vem da sua conta no Groq (só os Whisper). O turbo é mais rápido e o v3 erra um pouco menos.'}
          </div>

          {error && <div className="neo-error">Erro: {error}</div>}
          {ok && <div className="neo-ok">✓ {ok}</div>}
        </div>
      </div>

      <div className="neo-section">
        <h2 className="neo-h2">Cota gratuita agora</h2>
        <div className="neo-card">
          <div className="neo-meta">
            {linhaCota('Áudio nesta hora', cfg.cota.audio_hora, true)}
            {linhaCota('Áudio hoje', cfg.cota.audio_dia, true)}
            {linhaCota('Pedidos hoje', cfg.cota.pedidos_dia, false)}
            {linhaCota('Pedidos neste minuto', cfg.cota.pedidos_minuto, false)}
            <div>
              Arquivo por pedido: <span className="neo-meta-value">até {cfg.limites.arquivo_max_mb} MB (o Neo corta o áudio em pedaços)</span>
            </div>
          </div>
          <div className="neo-note">
            A conta é do Neo e enche aos poucos, como a do Groq. Ela se acerta pelo que o Groq informa em cada resposta.
            Os limites ficam em [transcricao.cota] no neo.toml.
          </div>
        </div>
      </div>
    </div>
  );
};

export default TranscricaoTab;
