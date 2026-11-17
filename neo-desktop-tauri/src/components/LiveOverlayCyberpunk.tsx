import './LiveOverlayCyberpunk.css';
import type { LiveState } from '../hooks/useLiveMode';

interface LiveOverlayCyberpunkProps {
  liveState: LiveState;
  partialText: string;
  spokenText: string;
  micLevel: number;
  onClose: () => void;
}

/**
 * Modo Live no visual "Cyberpunk 2077" (tema `glassdark`): HUD vermelho neon
 * sobre vidro quase-preto, com grid + scanline, núcleo em losango, brackets de
 * mira, equalizador reativo ao microfone e legenda estilo terminal.
 *
 * Os estágios são dirigidos pelo processo real (`liveState` do useLiveMode):
 * idle/listening → OUVINDO, thinking → PROCESSANDO, speaking → RESPONDENDO.
 * (Os 3 chips de troca de estágio do mockup do Claude Design foram removidos de
 * propósito — aqui quem alterna é o próprio ciclo de voz.)
 */
export default function LiveOverlayCyberpunk({
  liveState,
  partialText,
  spokenText,
  micLevel,
  onClose,
}: LiveOverlayCyberpunkProps) {
  const stage = liveState === 'idle' ? 'listening' : liveState;

  // O equalizador "respira" com o nível do microfone enquanto ouve. O 0.9 é o
  // teto de escala; o nível já chega normalizado 0…1 por `levelFromRms`, então
  // é multiplicação direta — é o que faz esta barra e o medidor da ilha
  // subirem na mesma proporção para a mesma voz.
  const eqScale = stage === 'listening' ? 1 + 0.9 * Math.min(1, micLevel) : 1;

  return (
    <div className={`cyber-live cyber-live-${stage}`}>
      <div className="cyber-grid" aria-hidden="true" />
      <div className="cyber-scan" aria-hidden="true" />

      <div className="cyber-live-label">MODO LIVE</div>

      <div className="cyber-live-stage">
        {stage === 'listening' && (
          <div className="cyber-col">
            <div className="cyber-core-wrap">
              <span className="cyber-bracket tl" />
              <span className="cyber-bracket tr" />
              <span className="cyber-bracket bl" />
              <span className="cyber-bracket br" />
              <span className="cyber-ring cyber-l-ring1" />
              <span className="cyber-ring cyber-l-ring2" />
              <span className="cyber-diamond" />
            </div>

            <div
              className="cyber-eq"
              style={{ transform: `scaleY(${eqScale})` }}
              aria-hidden="true"
            >
              <span />
              <span />
              <span />
              <span />
              <span />
            </div>

            <div className="cyber-state-block">
              <div className="cyber-state-line flicker">// OUVINDO</div>
              <div className="cyber-term">
                {partialText ? `> ${partialText}_` : '> aguardando input de voz_'}
              </div>
            </div>
          </div>
        )}

        {stage === 'thinking' && (
          <div className="cyber-col">
            <div className="cyber-core-wrap">
              <span className="cyber-ring cyber-t-ring1" />
              <span className="cyber-ring cyber-t-ring2" />
              <span className="cyber-ring cyber-t-ring3" />
              <span className="cyber-orb" />
            </div>

            <div className="cyber-state-block">
              <div className="cyber-state-line">
                <span>// PROCESSANDO</span>
                <span className="cyber-dots">
                  <span>.</span>
                  <span>.</span>
                  <span>.</span>
                </span>
              </div>
              <div className="cyber-term">&gt; analisando contexto e gerando resposta_</div>
            </div>
          </div>
        )}

        {stage === 'speaking' && (
          <div className="cyber-col cyber-col-say">
            <div className="cyber-core-wrap cyber-core-sm">
              <span className="cyber-ring cyber-s-ring" />
              <span className="cyber-diamond cyber-diamond-sm" />
            </div>

            <div className="cyber-say">
              <div className="cyber-say-label">// RESPONDENDO</div>
              <div className="cyber-say-text">
                {spokenText}
                <span className="cyber-cursor">▊</span>
              </div>
            </div>
          </div>
        )}
      </div>

      <button
        type="button"
        className="cyber-end"
        onClick={onClose}
        title="Encerrar Modo Live"
        aria-label="Encerrar Modo Live"
      >
        <span className="cyber-end-btn" aria-hidden="true">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
            <path d="M6 6l12 12M18 6L6 18" stroke="#0a0708" strokeWidth="2.4" strokeLinecap="round" />
          </svg>
        </span>
        <span className="cyber-end-label">Encerrar</span>
      </button>
    </div>
  );
}
