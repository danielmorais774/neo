import { useEffect, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import './NotificationWindow.css';

interface NotificationAction {
  action_type: string;
  label: string;
  payload?: Record<string, unknown>;
}

interface NotificationField {
  label: string;
  value: string;
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
  approval_required: '✋',
  mission_update: '💬',
  unsolicited_message: '📩',
  mission_started: '🚀',
  mission_completed: '✅',
};

const AUTO_DISMISS_MS = 6000;

export default function NotificationWindow() {
  const [notif, setNotif] = useState<NeoNotificationPayload | null>(null);
  const [visible, setVisible] = useState(false);

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

  const handleClick = () => {
    if (notif?.id) {
      invoke('ack_notification', { notificationId: notif.id }).catch(() => {});
    }
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
      className={`macos-notification ${visible ? 'visible' : 'hidden'}`}
      onClick={handleClick}
      role="alert"
    >
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
                <span className="field-value">{field.value}</span>
              </div>
            ))}
          </div>
        )}
        {notif.actions && notif.actions.length > 0 && (
          <div className="macos-notification-actions" onClick={(e) => e.stopPropagation()}>
            {notif.actions.map((action, i) => (
              <button
                key={i}
                className="macos-notification-action-btn"
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
