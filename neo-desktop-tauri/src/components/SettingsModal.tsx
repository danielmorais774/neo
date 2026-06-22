import type { AppTheme } from '../themes/themeTypes';
import { APP_THEMES, THEME_LABELS } from '../themes/themeTypes';
import VoiceEnginesPanel from './VoiceEnginesPanel';
import WakeWordPanel from './WakeWordPanel';
import { useWakeEnabled } from '../hooks';
import './SettingsModal.css';

interface SettingsModalProps {
  theme: AppTheme;
  onThemeChange: (theme: AppTheme) => void;
  onClose: () => void;
}

export default function SettingsModal({ theme, onThemeChange, onClose }: SettingsModalProps) {
  const [wakeEnabled, setWakeEnabled] = useWakeEnabled();
  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="settings-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="settings-modal-header">
          <div>
            <h2 id="settings-title">Configuracoes</h2>
            <p>Ajustes locais deste dispositivo.</p>
          </div>
          <button className="settings-close" type="button" onClick={onClose} aria-label="Fechar configuracoes">
            x
          </button>
        </div>

        <div className="settings-body">
          <div className="settings-section">
            <div className="settings-section-title">
              <span>Tema</span>
              <small>Aparencia da janela principal</small>
            </div>

            <div className="theme-options" role="radiogroup" aria-label="Tema visual">
              {APP_THEMES.map((option) => {
                const selected = theme === option;
                const label = THEME_LABELS[option];

                return (
                  <button
                    key={option}
                    type="button"
                    className={`theme-option ${selected ? 'selected' : ''}`}
                    role="radio"
                    aria-checked={selected}
                    onClick={() => onThemeChange(option)}
                  >
                    <span className={`theme-preview theme-preview-${option}`} />
                    <span className="theme-option-copy">
                      <strong>{label.title}</strong>
                      <small>{label.description}</small>
                    </span>
                    <span className="theme-check" aria-hidden="true">
                      {selected ? 'OK' : ''}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="settings-section">
            <div className="settings-section-title">
              <span>Motores de voz (PC gamer)</span>
              <small>Ligar/desligar STT e TTS na GPU do PC gamer</small>
            </div>
            <VoiceEnginesPanel />
          </div>

          <div className="settings-section">
            <div className="settings-section-title">
              <span>Palavra de despertar</span>
              <small>Diga "Ei Neo" para acender o Modo Live (mic local sempre ligado)</small>
            </div>
            <button
              type="button"
              className={`wake-toggle ${wakeEnabled ? 'on' : ''}`}
              role="switch"
              aria-checked={wakeEnabled}
              onClick={() => setWakeEnabled(!wakeEnabled)}
            >
              <span className="wake-toggle-track" aria-hidden="true">
                <span className="wake-toggle-thumb" />
              </span>
              <span className="wake-toggle-copy">
                <strong>{wakeEnabled ? 'Ativado' : 'Desativado'}</strong>
                <small>
                  {wakeEnabled
                    ? 'Escutando o gatilho. O ponto laranja do macOS fica aceso.'
                    : 'O microfone só liga ao entrar no Modo Live manualmente.'}
                </small>
              </span>
            </button>
            <WakeWordPanel />
          </div>
        </div>
      </section>
    </div>
  );
}
