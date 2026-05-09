import { useEffect, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import './NotificationWindow.css';

interface NeoNotificationPayload {
  id?: string;
  user_id?: string;
  mission_id?: string;
  kind?: string;
  title?: string;
  body?: string;
  payload?: Record<string, unknown>;
  created_at_unix?: number;
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
      dismissTimer = window.setTimeout(() => {
        setVisible(false);
        window.setTimeout(() => {
          getCurrentWindow().hide().catch(() => {});
        }, 280);
      }, AUTO_DISMISS_MS);
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
        {notif.body && <div className="macos-notification-body">{notif.body}</div>}
      </div>
    </div>
  );
}
