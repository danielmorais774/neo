import type { AppTheme } from '../themes/themeTypes';
import { APP_THEMES, THEME_LABELS } from '../themes/themeTypes';
import VoiceEnginesPanel from './VoiceEnginesPanel';
import './SettingsModal.css';

interface SettingsModalProps {
  theme: AppTheme;
  onThemeChange: (theme: AppTheme) => void;
  onClose: () => void;
}

export default function SettingsModal({ theme, onThemeChange, onClose }: SettingsModalProps) {
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
        </div>
      </section>
    </div>
  );
}
