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
};

const AUTO_DISMISS_MS = 6000;
const WINDOW_INSET_PX = 8;
const MIN_CARD_WIDTH = 280;
const MAX_CARD_WIDTH = 360;
const MIN_WINDOW_HEIGHT = 72;

export default function NotificationWindow() {
  const [notif, setNotif] = useState<NeoNotificationPayload | null>(null);
  const [visible, setVisible] = useState(false);
  const notificationRef = useRef<HTMLDivElement | null>(null);
  const lastWindowSizeRef = useRef<{ width: number; height: number } | null>(null);

  useEffect(() => {
    let unlistenFn: (() => void) | undefined;
    let dismissTimer: number | undefined;

    listen<NeoNotificationPayload>('notification', (event) => {
      setNotif(event.payload);
      setVisible(true);

      if (dismissTimer) window.clearTimeout(dismissTimer);

      const duration = event.payload.config?.persistent ? 0 : (event.payload.config?.duration_ms ?? AUTO_DISMISS_MS);
      if (duration > 0) {
        dismissTimer = window.setTimeout(() => {
          setVisible(false);
          window.setTimeout(() => {
            getCurrentWindow().hide().catch(() => {});
          }, 280);
        }, duration);
      }
    }).then((u) => {
      unlistenFn = u;
    });

    return () => {
      if (unlistenFn) unlistenFn();
      if (dismissTimer) window.clearTimeout(dismissTimer);
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
        actionPayload: action.payload ?? null,
      }).catch(() => {});
    }
    setVisible(false);
    window.setTimeout(() => {
      getCurrentWindow().hide().catch(() => {});
    }, 200);
  };

  if (!notif) return null;

  const icon = KIND_ICON[notif.kind || ''] || '🔔';

  return (
    <div
      ref={notificationRef}
      className={`macos-notification ${visible ? 'visible' : 'hidden'}`}
      onClick={handleClick}
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
