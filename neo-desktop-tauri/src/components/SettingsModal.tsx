import { createPortal } from 'react-dom';
import type { AppTheme } from '../themes/themeTypes';
import { APP_THEMES, THEME_LABELS } from '../themes/themeTypes';
import VoiceEnginesPanel from './VoiceEnginesPanel';
import AudioDevicesPanel from './AudioDevicesPanel';
import WakeWordPanel from './WakeWordPanel';
import WakeTestPanel from './WakeTestPanel';
import { useWakeEnabled } from '../hooks';
import './SettingsModal.css';

interface SettingsModalProps {
  theme: AppTheme;
  onThemeChange: (theme: AppTheme) => void;
  onClose: () => void;
}

export default function SettingsModal({ theme, onThemeChange, onClose }: SettingsModalProps) {
  const [wakeEnabled, setWakeEnabled] = useWakeEnabled();

  // O caderno abre em JANELA PRÓPRIA (comando `open_working_memory_window`), e
  // não como overlay: ler as notas é trabalho lado a lado com o chat — como
  // overlay, conferir o que o Neo anotou obrigava a fechar a tela para voltar à
  // conversa. Chamar com a janela já aberta apenas a traz para a frente.
  const openNotebook = () => {
    window.electronAPI?.openWorkingMemoryWindow().catch((err) => {
      console.error('[Neo] Falha ao abrir a janela do caderno:', err);
    });
  };
  // Portal para document.body: escapa do `.app`, que tem `overflow:hidden` e uma
  // -webkit-mask radial (desbota as bordas) e, no WebKit, vira containing block
  // do position:fixed — o que recortava/esmaecia o topo e o rodapé do modal.
  return createPortal(
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
              <span>Dispositivos de áudio</span>
              <small>Microfone e alto-falante usados no Modo Live</small>
            </div>
            <AudioDevicesPanel />
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

          <div className="settings-section">
            <div className="settings-section-title">
              <span>Memória de trabalho</span>
              <small>O caderno da tarefa — o que o Neo anota entre um turno e o outro</small>
            </div>
            <button type="button" className="settings-open-screen" onClick={openNotebook}>
              <span className="settings-open-screen-icon" aria-hidden="true">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 19.5V5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v14H6a2 2 0 0 0-2 2z" />
                  <line x1="8" y1="7" x2="15" y2="7" />
                  <line x1="8" y1="11" x2="15" y2="11" />
                  <line x1="8" y1="15" x2="12" y2="15" />
                </svg>
              </span>
              <span className="settings-open-screen-copy">
                <strong>Abrir o caderno</strong>
                <small>Preview do markdown de cada nota, direto do neo-core</small>
              </span>
              <span className="settings-open-screen-chevron" aria-hidden="true">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </span>
            </button>
          </div>

          <div className="settings-section">
            <div className="settings-section-title">
              <span>Teste de Wake</span>
              <small>Veja o score de cada fala p/ afinar o limiar e o gatilho</small>
            </div>
            <WakeTestPanel />
          </div>
        </div>
      </section>
    </div>,
    document.body,
  );
}
