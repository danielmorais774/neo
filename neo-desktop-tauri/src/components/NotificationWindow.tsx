import { useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import './NotificationWindow.css';

interface NotificationAction {
  action_type: string;
  label: string;
  payload?: Record<string, unknown>;
  button_style?: 'contained' | 'outlined' | 'text' | 'ghost';
}

interface NotificationField {
  label: string;
  value: string;
  field_type?: 'text' | 'select';
  options?: string;
  input_value?: string;
}

interface NotificationConfig {
  duration_ms?: number;
  position?: 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right' | 'center';
  persistent?: boolean;
  play_sound?: boolean;
}

interface NeoNotificationPayload {
  id?: string;
  user_id?: string;
  mission_id?: string;
  kind?: string;
  title?: string;
  description?: string;
  fields?: NotificationField[];
  actions?: NotificationAction[];
  payload?: Record<string, unknown>;
  created_at_unix?: number;
  config?: NotificationConfig;
}

const KIND_ICON: Record<string, string> = {
  approval_required: '🤖',
  mission_update: '💬',
  unsolicited_message: '📩',
  mission_started: '⚡',
  mission_completed: '✨',
  // Liberação de dado pessoal: ícone próprio porque este card não é aviso, é
  // decisão — e a diferença tem que ser visível antes de ler (release-request-plan §11).
  release_request: '🔐',
};

/*
 * Auto-dispensa consciente de presença — a MESMA regra do host nativo em Swift
 * (src-tauri/macos/NeoNotificationHost.swift). Esta janela é o fallback (e o
 * caminho único fora do macOS); duas superfícies com prazos diferentes seriam um
 * bug esperando a vez, então os números e a política moram nos dois lugares com
 * o mesmo valor.
 *
 * A regra: o prazo só corre quando há alguém na frente do computador. Sem input
 * recente o aviso espera — quem saiu para o café volta e encontra o recado, em
 * vez de descobrir depois que perdeu.
 */
const AUTO_DISMISS_MS = 8000;
/** Sem input por mais que isto = usuário longe; o relógio congela. */
const IDLE_THRESHOLD_S = 30;
/** Resolução do relógio de presença. */
const PRESENCE_TICK_MS = 500;
const WINDOW_INSET_PX = 8;
const MIN_CARD_WIDTH = 280;
const MAX_CARD_WIDTH = 360;
const MIN_WINDOW_HEIGHT = 72;

export default function NotificationWindow() {
  const [notif, setNotif] = useState<NeoNotificationPayload | null>(null);
  const [visible, setVisible] = useState(false);
  const notificationRef = useRef<HTMLDivElement | null>(null);
  /** Ponteiro em cima do cartão: congela o prazo enquanto se lê. */
  const hoverRef = useRef(false);
  const lastWindowSizeRef = useRef<{ width: number; height: number } | null>(null);

  useEffect(() => {
    let unlistenFn: (() => void) | undefined;
    let tick: number | undefined;
    let restanteMs = 0;
    let ultimoTique = 0;

    const esconder = () => {
      setVisible(false);
      window.setTimeout(() => {
        getCurrentWindow().hide().catch(() => {});
      }, 280);
    };

    const pararRelogio = () => {
      if (tick) window.clearInterval(tick);
      tick = undefined;
    };

    const rodarRelogio = () => {
      pararRelogio();
      ultimoTique = Date.now();
      tick = window.setInterval(async () => {
        const agora = Date.now();
        const decorrido = agora - ultimoTique;
        ultimoTique = agora;
        // Mouse em cima = está lendo; ler é uso, o prazo para.
        if (hoverRef.current) return;
        let ocioso = 0;
        try {
          ocioso = await invoke<number>('system_idle_seconds');
        } catch {
          ocioso = 0; // sem o comando, trata como presente (cronômetro comum)
        }
        if (ocioso >= IDLE_THRESHOLD_S) return; // ninguém na frente: segura
        restanteMs -= decorrido;
        if (restanteMs <= 0) {
          pararRelogio();
          esconder();
        }
      }, PRESENCE_TICK_MS);
    };

    listen<NeoNotificationPayload>('notification', (event) => {
      setNotif(event.payload);
      setVisible(true);
      hoverRef.current = false;

      // Quem PERGUNTA alguma coisa fica na tela: um pedido de aprovação que some
      // sozinho é uma decisão perdida. O resto é aviso, e aviso é passageiro.
      const pedeResposta =
        (event.payload.actions?.length ?? 0) > 0 || (event.payload.fields?.length ?? 0) > 0;
      const persistente = event.payload.config?.persistent ?? pedeResposta;
      const duracao = persistente ? 0 : (event.payload.config?.duration_ms ?? AUTO_DISMISS_MS);

      pararRelogio();
      if (duracao > 0) {
        restanteMs = duracao;
        rodarRelogio();
      }
    }).then((u) => {
      unlistenFn = u;
    });

    return () => {
      if (unlistenFn) unlistenFn();
      pararRelogio();
    };
  }, []);

  // Resize the native window to the rendered notification card.
  useEffect(() => {
    if (!notif) return;

    lastWindowSizeRef.current = null;

    let animationFrame: number | undefined;

    const resizeWindow = () => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);

      animationFrame = window.requestAnimationFrame(() => {
        const el = notificationRef.current;
        if (!el) return;

        const rect = el.getBoundingClientRect();
        const cardWidth = Math.ceil(Math.max(rect.width, el.scrollWidth));
        const cardHeight = Math.ceil(Math.max(rect.height, el.scrollHeight));
        const maxWindowHeight = Math.max(
          MIN_WINDOW_HEIGHT,
          Math.min(Math.floor(window.screen.availHeight - 48), 720),
        );
        const nextWidth = Math.ceil(
          Math.min(Math.max(cardWidth, MIN_CARD_WIDTH), MAX_CARD_WIDTH) + WINDOW_INSET_PX * 2,
        );
        const nextHeight = Math.ceil(
          Math.min(Math.max(cardHeight + WINDOW_INSET_PX * 2, MIN_WINDOW_HEIGHT), maxWindowHeight),
        );

        const previous = lastWindowSizeRef.current;
        if (previous?.width === nextWidth && previous?.height === nextHeight) return;

        lastWindowSizeRef.current = { width: nextWidth, height: nextHeight };
        invoke('resize_notification_window', {
          height: nextHeight,
          width: nextWidth,
          position: notif.config?.position ?? 'bottom_right',
        }).catch(console.error);
      });
    };

    const observer = new ResizeObserver(() => {
      resizeWindow();
    });

    const el = notificationRef.current;
    if (el) observer.observe(el);

    resizeWindow();
    const settleTimeout = window.setTimeout(resizeWindow, 320);
    window.addEventListener('resize', resizeWindow);

    return () => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
      window.clearTimeout(settleTimeout);
      observer.disconnect();
      window.removeEventListener('resize', resizeWindow);
    };
  }, [notif]);

  const handleClick = () => {
    if (notif?.id) {
      invoke('ack_notification', { notificationId: notif.id }).catch(() => {});
    }
    setVisible(false);
    window.setTimeout(() => {
      getCurrentWindow().hide().catch(() => {});
    }, 200);
  };

  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    setVisible(false);
    window.setTimeout(() => {
      getCurrentWindow().hide().catch(() => {});
    }, 200);
  };

  const handleActionClick = (e: React.MouseEvent, action: NotificationAction) => {
    e.stopPropagation();
    if (notif?.id) {
      invoke('handle_notification_action', {
        notificationId: notif.id,
        actionType: action.action_type,
        actionPayload: {
          ...(notif.payload ?? {}),
          ...(action.payload ?? {}),
          mission_id: notif.mission_id ?? notif.payload?.mission_id,
        },
      }).catch(() => {});
    }
    setVisible(false);
    window.setTimeout(() => {
      getCurrentWindow().hide().catch(() => {});
    }, 200);
  };

  if (!notif) return null;

  const icon = KIND_ICON[notif.kind || ''] || '🔔';
  const draftMessages = Array.isArray(notif.payload?.draft_messages)
    ? notif.payload.draft_messages.map(String).filter(Boolean)
    : [];

  return (
    <div
      ref={notificationRef}
      className={`macos-notification ${visible ? 'visible' : 'hidden'}`}
      onClick={handleClick}
      onMouseEnter={() => { hoverRef.current = true; }}
      onMouseLeave={() => { hoverRef.current = false; }}
      role="alert"
    >
      <button
        className="macos-notification-close"
        onClick={handleClose}
        aria-label="Fechar notificação"
      >
        ×
      </button>
      <div className="macos-notification-icon">
        <div className="icon-bubble">{icon}</div>
      </div>
      <div className="macos-notification-content">
        <div className="macos-notification-header">
          <span className="macos-notification-app">Neo Assistant</span>
          <span className="macos-notification-time">agora</span>
        </div>
        <div className="macos-notification-title">{notif.title || 'Nova notificação'}</div>
        {notif.description && <div className="macos-notification-body">{notif.description}</div>}
        {draftMessages.length > 1 && (
          <div className="macos-notification-fields">
            {draftMessages.map((message, index) => (
              <div key={index} className="macos-notification-field">
                <span className="field-label">{index + 1}</span>
                <span className="field-value">{message}</span>
              </div>
            ))}
          </div>
        )}
        {notif.fields && notif.fields.length > 0 && (
          <div className="macos-notification-fields">
            {notif.fields.map((field, i) => (
              <div key={i} className="macos-notification-field">
                <span className="field-label">{field.label}</span>
                {field.field_type === 'select' ? (
                  <select
                    className="field-input field-select"
                    defaultValue={field.value}
                    onChange={(e) => {
                      if (notif) {
                        const updated = { ...notif };
                        if (updated.fields) {
                          updated.fields[i] = { ...updated.fields[i], input_value: e.target.value };
                          setNotif(updated);
                        }
                      }
                    }}
                  >
                    {(field.options || '').split(',').filter(o => o.trim()).map(opt => (
                      <option key={opt} value={opt.trim()}>{opt.trim()}</option>
                    ))}
                  </select>
                ) : field.field_type === 'text' ? (
                  <input
                    type="text"
                    className="field-input field-text"
                    defaultValue={field.value}
                    onChange={(e) => {
                      if (notif) {
                        const updated = { ...notif };
                        if (updated.fields) {
                          updated.fields[i] = { ...updated.fields[i], input_value: e.target.value };
                          setNotif(updated);
                        }
                      }
                    }}
                  />
                ) : (
                  <span className="field-value">{field.value}</span>
                )}
              </div>
            ))}
          </div>
        )}
        {notif.actions && notif.actions.length > 0 && (
          <div className="macos-notification-actions" onClick={(e) => e.stopPropagation()}>
            {notif.actions.map((action, i) => (
              <button
                key={i}
                className={`macos-notification-action-btn${action.button_style ? ` ${action.button_style}` : ''}`}
                onClick={(e) => handleActionClick(e, action)}
              >
                {action.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
