import './LiveOverlay.css';
import type { LiveState } from '../hooks/useLiveMode';

interface LiveOverlayProps {
  liveState: LiveState;
  partialText: string;
  spokenText: string;
  micLevel: number;
  onClose: () => void;
}

const STATUS: Record<LiveState, string> = {
  idle: '',
  listening: 'Ouvindo',
  thinking: 'Pensando',
  speaking: 'Neo está falando',
};

/**
 * Tela cheia de "chamada de voz" (estilo Gemini Live): orbe animado que reage
 * ao áudio, legenda do que está sendo dito e botão de encerrar. Cobre a área de
 * conteúdo enquanto o Modo Live está ativo.
 */
export default function LiveOverlay({
  liveState,
  partialText,
  spokenText,
  micLevel,
  onClose,
}: LiveOverlayProps) {
  // o orbe "respira" com o nível do microfone enquanto ouve
  const scale = liveState === 'listening' ? 1 + Math.min(0.45, micLevel * 0.6) : 1;

  const caption =
    liveState === 'speaking' ? spokenText : liveState === 'listening' ? partialText : '';

  return (
    <div className={`live-overlay live-${liveState}`}>
      <div className="live-overlay-header">
        <span className="live-overlay-title">Modo Live</span>
      </div>

      <div className="live-overlay-center">
        <div className="live-orb-wrap">
          <span className="live-orb-ring ring1" />
          <span className="live-orb-ring ring2" />
          <span className="live-orb-ring ring3" />
          <span className="live-orb" style={{ transform: `scale(${scale})` }} />
        </div>

        <div className="live-overlay-status">{STATUS[liveState]}</div>

        <div className={`live-overlay-caption${liveState === 'speaking' ? ' from-neo' : ''}`}>
          {liveState === 'thinking' ? (
            <span className="live-thinking-dots">
              <span />
              <span />
              <span />
            </span>
          ) : caption ? (
            caption
          ) : (
            <span className="live-overlay-hint">Pode falar…</span>
          )}
        </div>
      </div>

      <div className="live-overlay-controls">
        <button
          type="button"
          className="live-end-btn"
          onClick={onClose}
          title="Encerrar Modo Live"
          aria-label="Encerrar Modo Live"
        >
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="6" y1="6" x2="18" y2="18" />
            <line x1="18" y1="6" x2="6" y2="18" />
          </svg>
        </button>
        <span className="live-end-label">Encerrar</span>
      </div>
    </div>
  );
}
