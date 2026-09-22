import { useEffect, useState } from 'react';

// Aba Arquivos: quem altera arquivos de texto do computador do usuário.
// Backend: neo-core/api/routes/file_edit_config.py (lido a cada turno pelo core).

type Engine = 'neo' | 'opencode';

interface FileEditConfig {
  engine: Engine;
  opencode_model: string;
  opencode_timeout_s: number;
}

interface Payload {
  current: FileEditConfig;
  defaults: FileEditConfig;
  engines: string[];
}

const ArquivosTab = ({ reloadToken }: { reloadToken: number }) => {
  const [data, setData] = useState<Payload | null>(null);
  const [engine, setEngine] = useState<Engine>('neo');
  const [model, setModel] = useState('');
  const [timeoutS, setTimeoutS] = useState(300);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const apply = (p: Payload) => {
    setData(p);
    setEngine(p.current.engine);
    setModel(p.current.opencode_model);
    setTimeoutS(p.current.opencode_timeout_s);
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch('/api/v1/file-edit-config');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const p: Payload = await res.json();
        if (alive) apply(p);
      } catch (e) {
        if (alive) setError(String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  const dirty =
    !!data &&
    (engine !== data.current.engine ||
      model.trim() !== data.current.opencode_model ||
      timeoutS !== data.current.opencode_timeout_s);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const res = await fetch('/api/v1/file-edit-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          engine,
          opencode_model: model.trim(),
          opencode_timeout_s: timeoutS,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      apply(await res.json());
      setSuccess('Salvo — vale a partir da próxima mensagem.');
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="neo-content">
      <div className="neo-section">
        <h2 className="neo-h2">Edição de arquivos</h2>
        <p className="neo-desc">
          Quem altera arquivos de texto do seu computador quando você pede ("atualiza esse .md",
          "corrige esse script"). Nos dois modos o desktop lê o arquivo direto do seu disco — nada
          fica guardado na VPS — e toda alteração aparece no chat com o diff e um botão Desfazer.
        </p>

        <div className="neo-card">
          <div className="neo-field">
            <label className="neo-field-label">Quem edita</label>
            <div className="neo-select-wrap">
              <select
                className="neo-select"
                value={engine}
                onChange={(e) => {
                  setEngine(e.target.value as Engine);
                  setSuccess(null);
                }}
              >
                <option value="neo">Neo (ferramentas próprias)</option>
                <option value="opencode">opencode (o Neo orquestra)</option>
              </select>
              <span className="neo-chevron">▼</span>
            </div>
          </div>

          <p className="neo-desc">
            {engine === 'neo'
              ? 'O Neo lê o arquivo e troca só os trechos necessários, conferindo que ele não mudou desde a leitura.'
              : 'O Neo escreve a instrução, o opencode instalado no seu computador edita uma cópia, o desktop aplica o resultado e o Neo confere o diff. O opencode roda sem shell, sem internet e sem ler .env. Se ele não estiver instalado na máquina, o Neo usa as ferramentas próprias.'}
          </p>

          {engine === 'opencode' && (
            <>
              <div className="neo-field">
                <label className="neo-field-label">Modelo do opencode</label>
                <input
                  className="neo-input"
                  value={model}
                  placeholder={data?.defaults.opencode_model}
                  onChange={(e) => {
                    setModel(e.target.value);
                    setSuccess(null);
                  }}
                />
              </div>
              <div className="neo-field">
                <label className="neo-field-label">Tempo limite (segundos)</label>
                <input
                  className="neo-input"
                  type="number"
                  min={30}
                  max={1800}
                  value={timeoutS}
                  onChange={(e) => {
                    setTimeoutS(Number(e.target.value) || 300);
                    setSuccess(null);
                  }}
                />
              </div>
              <div className="neo-meta">
                <div>
                  Formato <span className="neo-meta-value">provedor/modelo</span> (ex.:{' '}
                  <span className="neo-meta-value">openrouter/deepseek/deepseek-v4.1-flash</span>). A
                  credencial fica no próprio opencode (<span className="neo-meta-value">opencode auth</span>).
                </div>
              </div>
            </>
          )}

          <div className="neo-actions">
            <button className="neo-btn-primary" onClick={() => void save()} disabled={!dirty || saving}>
              {saving ? 'Salvando...' : 'Salvar'}
            </button>
            {dirty && <span className="neo-note">Alteração não salva</span>}
          </div>

          {error && <div className="neo-error">Erro: {error}</div>}
          {success && <div className="neo-ok">✓ {success}</div>}
        </div>
      </div>

      <div className="neo-section">
        <h2 className="neo-h2">Regras do desktop</h2>
        <p className="neo-desc">
          Valem nos dois modos e ficam no app do desktop — o Neo não consegue mudá-las por conversa.
        </p>
        <div className="neo-card">
          <div className="neo-meta">
            <div>
              Desktop, Documentos e Downloads: <span className="neo-meta-value">aplica direto</span>
            </div>
            <div>
              Outras pastas e caminhos sensíveis (~/.ssh, .zshrc, .env):{' '}
              <span className="neo-meta-value">pede aprovação no chat</span>
            </div>
            <div>
              Pastas do sistema (/etc, /System, /usr…): <span className="neo-meta-value">bloqueado</span>
            </div>
            <div>
              Toda alteração: <span className="neo-meta-value">backup + Desfazer</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ArquivosTab;
