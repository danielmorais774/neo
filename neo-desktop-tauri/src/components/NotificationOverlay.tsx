import { useEffect, useState } from 'react';
import type { NeoNotification } from '../types/electron';
import './NotificationOverlay.css';

interface ToastItem {
  notification: NeoNotification;
  receivedAt: number;
}

interface NotificationOverlayProps {
  onReply?: (notification: NeoNotification) => void;
}

const KIND_ICON: Record<string, string> = {
  approval_required: '✋',
  mission_update: '💬',
  unsolicited_message: '📩',
  mission_started: '🚀',
  mission_completed: '✅',
};

const AUTO_DISMISS_MS = 12000;

export default function NotificationOverlay({ onReply }: NotificationOverlayProps) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  useEffect(() => {
    if (!window.electronAPI?.onNotification) return;
    window.electronAPI.onNotification((notif) => {
      setToasts((prev) => {
        if (prev.some((t) => t.notification.id === notif.id)) return prev;
        return [...prev, { notification: notif, receivedAt: Date.now() }];
      });
    });
  }, []);

  useEffect(() => {
    if (toasts.length === 0) return;
    const timer = setInterval(() => {
      const now = Date.now();
      setToasts((prev) => prev.filter((t) => now - t.receivedAt < AUTO_DISMISS_MS));
    }, 1000);
    return () => clearInterval(timer);
  }, [toasts.length]);

  const dismiss = (id: string) => {
    setToasts((prev) => prev.filter((t) => t.notification.id !== id));
    window.electronAPI?.ackNotification?.(id);
  };

  const handleReply = (notif: NeoNotification) => {
    onReply?.(notif);
    dismiss(notif.id);
  };

  if (toasts.length === 0) return null;

  return (
    <div className="notification-overlay" role="region" aria-label="Notificações">
      {toasts.map(({ notification }) => {
        const icon = KIND_ICON[notification.kind] || '🔔';
        const isReplyable =
          notification.kind === 'mission_update' ||
          notification.kind === 'unsolicited_message';
        return (
          <div
            key={notification.id}
            className={`notification-card kind-${notification.kind}`}
            role="alert"
          >
            <div className="notification-icon">{icon}</div>
            <div className="notification-content">
              <div className="notification-title">{notification.title}</div>
              <div className="notification-body">{notification.description}</div>
            </div>
            <div className="notification-actions">
              {isReplyable && (
                <button
                  className="notif-btn notif-btn-primary"
                  onClick={() => handleReply(notification)}
                >
                  Responder
                </button>
              )}
              <button
                className="notif-btn"
                onClick={() => dismiss(notification.id)}
                aria-label="Dispensar"
              >
                ✕
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
