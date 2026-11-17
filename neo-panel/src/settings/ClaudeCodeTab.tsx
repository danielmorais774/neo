import { useEffect, useState } from 'react';

// Aba Claude Code: liga e desliga o Claude Code integrado ao Neo.
// Backend: neo-core/api/routes/claude_code_config.py — vale na hora, sem reiniciar
// (neo-core/core/claude_code/chave.py). O CLAUDE_CODE_ENABLED do .env é só o padrão
// de quando o painel nunca mexeu.

interface ClaudeCodeConfig {
  enabled: boolean;
  /** "env" = vale o .env (o painel nunca gravou); "painel" = gravado aqui. */
  source: 'env' | 'painel';
  env_default: boolean;
  /** O runner está montado no core (precisa de JOBS_ENABLED). */
  available: boolean;
  /** Tarefas do Claude na fila, rodando ou esperando resposta. */
  active_tasks: number;
}

const ClaudeCodeTab = ({ reloadToken }: { reloadToken: number }) => {
  const [cfg, setCfg] = useState<ClaudeCodeConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch('/api/v1/claude-code-config');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: ClaudeCodeConfig = await res.json();
        if (alive) {
          setCfg(data);
          setError(null);
        }
      } catch (e) {
        if (alive) setError(String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  const toggle = async () => {
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/claude-code-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !cfg.enabled }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      setCfg(await res.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const tarefas = cfg?.active_tasks ?? 0;

  return (
    <div className="neo-content">
      <div className="neo-section">
        <h2 className="neo-h2">Claude Code integrado</h2>
        <p className="neo-desc">
          Ligado, o Neo pode passar uma tarefa ao Claude Code do seu Mac quando você pedir ("pede pro
          Claude…"), acompanhar as rodadas e trazer o relatório. Usa a cota da sua assinatura do Claude.
          Desligado, o Neo não oferece nem executa nada do Claude. Vale na hora, sem reiniciar.
        </p>

        <div className="neo-card">
          {!cfg ? (
            <div className="neo-note">{error ? `Não foi possível ler a configuração: ${error}` : 'Lendo configuração...'}</div>
          ) : (
            <>
              <div className="neo-toggle-row">
                <div className="neo-toggle-label">
                  Claude Code:{' '}
                  <span className={cfg.enabled ? 'neo-state-on' : 'neo-state-off'}>
                    {cfg.enabled ? 'HABILITADO' : 'DESABILITADO'}
                  </span>
                  {tarefas > 0 && (
                    <span className="neo-state-live">
                      ● {tarefas} {tarefas === 1 ? 'tarefa em andamento' : 'tarefas em andamento'}
                    </span>
                  )}
                </div>
                <button
                  className={`neo-toggle ${cfg.enabled ? 'is-on' : 'is-off'}`}
                  onClick={() => void toggle()}
                  disabled={saving || (!cfg.enabled && !cfg.available)}
                >
                  {saving ? '...' : cfg.enabled ? '● LIGADO' : '○ DESLIGADO'}
                </button>
              </div>

              {!cfg.available && (
                <div className="neo-error">
                  O core não montou o Claude Code (JOBS_ENABLED está desligado no .env), então não dá para ligar daqui.
                </div>
              )}
              {!cfg.enabled && tarefas > 0 && (
                <p className="neo-desc">
                  O que já está rodando termina sozinho. Enquanto estiver desligado, o Neo não manda respostas nem
                  ajustes para essas tarefas.
                </p>
              )}

              <div className="neo-meta">
                <div>
                  Quem decide agora:{' '}
                  <span className="neo-meta-value">
                    {cfg.source === 'painel'
                      ? 'este painel'
                      : `o .env (CLAUDE_CODE_ENABLED=${cfg.env_default ? 'true' : 'false'}), até você mudar aqui`}
                  </span>
                </div>
              </div>
              {error && <div className="neo-error">Erro: {error}</div>}
            </>
          )}
        </div>
      </div>

      <div className="neo-section">
        <h2 className="neo-h2">Como funciona quando ligado</h2>
        <div className="neo-card">
          <div className="neo-meta">
            <div>
              Onde roda: <span className="neo-meta-value">o Claude Code do seu Mac, com o login que você já fez</span>
            </div>
            <div>
              Primeira rodada de cada tarefa: <span className="neo-meta-value">confirmação na tela do Mac</span>
            </div>
            <div>
              Permissões: <span className="neo-meta-value">modo auto — o arriscado vira pergunta no chat</span>
            </div>
            <div>
              Ajuste depois ("pede pro Claude…"): <span className="neo-meta-value">mesma sessão, mesma pasta</span>
            </div>
            <div>
              Acompanhar: <span className="neo-meta-value">faixa acima do campo de mensagem e botão no balão</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ClaudeCodeTab;
