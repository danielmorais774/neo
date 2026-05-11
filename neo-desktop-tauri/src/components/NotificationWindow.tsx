import { useCallback, useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
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
  source: string;
  placeholder?: string;
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
  confirm_reply: '✦',
  mission_update: '💬',
  unsolicited_message: '📩',
  mission_started: '⚡',
  mission_completed: '✨',
};

const AUTO_DISMISS_MS = 12000;
const WINDOW_INSET_PX = 8;
const MIN_CARD_WIDTH = 280;
const MAX_CARD_WIDTH = 360;
const MIN_WINDOW_HEIGHT = 72;

export default function NotificationWindow() {
  const [notif, setNotif] = useState<NeoNotificationPayload | null>(null);
  const [visible, setVisible] = useState(false);
  const [dismissProgress, setDismissProgress] = useState(1);
  const [dismissPaused, setDismissPaused] = useState(false);
  const [editingReply, setEditingReply] = useState(false);
  const [manualReplyText, setManualReplyText] = useState('');
  const notificationRef = useRef<HTMLDivElement | null>(null);
  const manualReplyInputRef = useRef<HTMLInputElement | null>(null);
  const lastWindowSizeRef = useRef<{ width: number; height: number } | null>(null);
  const dismissTimerRef = useRef<number | undefined>(undefined);
  const dismissLastTickAtRef = useRef<number | undefined>(undefined);
  const dismissDurationMsRef = useRef(0);
  const dismissRemainingMsRef = useRef(0);
  const dismissPausedRef = useRef(false);
  const pointerHoverRef = useRef(false);
  const nativeHoverRef = useRef(false);
  const nativeHoverCheckInFlightRef = useRef(false);
  const hideTimerRef = useRef<number | undefined>(undefined);
  const closeSequenceRef = useRef(0);

  const clearDismissTimer = useCallback(() => {
    if (dismissTimerRef.current) {
      window.clearInterval(dismissTimerRef.current);
      dismissTimerRef.current = undefined;
    }
    dismissLastTickAtRef.current = undefined;
  }, []);

  const resetDismissState = useCallback(() => {
    clearDismissTimer();
    dismissDurationMsRef.current = 0;
    dismissRemainingMsRef.current = 0;
    dismissPausedRef.current = false;
    pointerHoverRef.current = false;
    nativeHoverRef.current = false;
    nativeHoverCheckInFlightRef.current = false;
    setDismissProgress(1);
    setDismissPaused(false);
  }, [clearDismissTimer]);

  const resetReplyEditor = useCallback(() => {
    setEditingReply(false);
    setManualReplyText('');
    invoke('set_notification_window_focusable', { focusable: false }).catch(() => {});
  }, []);

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current) {
      window.clearTimeout(hideTimerRef.current);
      hideTimerRef.current = undefined;
    }
  }, []);

  const closeNotification = useCallback((delayMs = 300) => {
    resetDismissState();
    resetReplyEditor();
    clearHideTimer();
    const closeSequence = closeSequenceRef.current + 1;
    closeSequenceRef.current = closeSequence;
    setVisible(false);
    invoke('set_notification_window_interactive', { interactive: false }).catch(() => {});
    hideTimerRef.current = window.setTimeout(() => {
      invoke('hide_notification_window')
        .catch(() => {})
        .finally(() => {
          if (closeSequenceRef.current !== closeSequence) return;
          hideTimerRef.current = undefined;
          setNotif(null);
        });
    }, delayMs);
  }, [clearHideTimer, resetDismissState, resetReplyEditor]);

  const startDismissTimer = useCallback((durationMs: number) => {
    clearDismissTimer();
    if (durationMs <= 0) {
      dismissDurationMsRef.current = 0;
      dismissRemainingMsRef.current = 0;
      setDismissProgress(1);
      return;
    }

    dismissDurationMsRef.current = durationMs;
    dismissRemainingMsRef.current = durationMs;
    dismissPausedRef.current = false;
    pointerHoverRef.current = false;
    nativeHoverRef.current = false;
    dismissLastTickAtRef.current = Date.now();
    setDismissProgress(1);
    setDismissPaused(false);

    dismissTimerRef.current = window.setInterval(() => {
      const now = Date.now();
      const elapsedMs = now - (dismissLastTickAtRef.current ?? now);
      dismissLastTickAtRef.current = now;

      if (!nativeHoverCheckInFlightRef.current) {
        nativeHoverCheckInFlightRef.current = true;
        invoke<boolean>('is_cursor_over_notification_window')
          .then((hovered) => {
            nativeHoverRef.current = hovered;
          })
          .catch(() => {
            nativeHoverRef.current = false;
          })
          .finally(() => {
            nativeHoverCheckInFlightRef.current = false;
          });
      }

      const isPaused = pointerHoverRef.current || nativeHoverRef.current;
      if (dismissPausedRef.current !== isPaused) {
        dismissPausedRef.current = isPaused;
        setDismissPaused(isPaused);
      }

      if (isPaused) return;

      const remainingMs = Math.max(0, dismissRemainingMsRef.current - elapsedMs);
      dismissRemainingMsRef.current = remainingMs;
      setDismissProgress(Math.max(0, Math.min(1, remainingMs / durationMs)));

      if (remainingMs <= 0) {
        closeNotification();
      }
    }, 100);
  }, [clearDismissTimer, closeNotification]);

  const pauseDismissTimer = useCallback(() => {
    pointerHoverRef.current = true;
    dismissPausedRef.current = true;
    setDismissPaused(true);
  }, []);

  const resumeDismissTimer = useCallback(() => {
    pointerHoverRef.current = false;
  }, []);

  useEffect(() => {
    let unlistenFn: (() => void) | undefined;

    listen<NeoNotificationPayload>('notification', (event) => {
      resetDismissState();
      resetReplyEditor();
      clearHideTimer();
      closeSequenceRef.current += 1;
      invoke('set_notification_window_interactive', { interactive: true }).catch(() => {});

      setNotif(event.payload);
      setVisible(true);

      const duration = event.payload.config?.persistent ? 0 : (event.payload.config?.duration_ms ?? AUTO_DISMISS_MS);
      if (duration > 0) {
        startDismissTimer(duration);
      }
    }).then((u) => {
      unlistenFn = u;
    });

    return () => {
      if (unlistenFn) unlistenFn();
      clearDismissTimer();
      clearHideTimer();
    };
  }, [clearDismissTimer, clearHideTimer, resetDismissState, resetReplyEditor, startDismissTimer]);

  useEffect(() => {
    if (!editingReply) return;

    const timer = window.setTimeout(() => {
      manualReplyInputRef.current?.focus();
    }, 60);

    return () => window.clearTimeout(timer);
  }, [editingReply]);

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

  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (notif?.id) {
      invoke('ack_notification', { notificationId: notif.id }).catch(() => {});
    }
    closeNotification();
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
    closeNotification();
  };

  const getMissionId = useCallback(() => {
    const payloadMissionId = notif?.payload?.mission_id;
    return typeof payloadMissionId === 'string' ? payloadMissionId : notif?.mission_id || '';
  }, [notif]);

  const submitConfirmReply = useCallback(async (approved: boolean, editedText?: string) => {
    const missionId = getMissionId();
    if (!missionId) {
      closeNotification();
      return;
    }

    const endpoint = approved ? 'approve' : 'cancel';
    const body = approved && editedText ? JSON.stringify({ edited_text: editedText }) : '{}';
    try {
      await fetch(`http://localhost:8000/api/v1/missions/${missionId}/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (notif?.id) {
        invoke('ack_notification', { notificationId: notif.id }).catch(() => {});
      }
    } catch (error) {
      console.error('[NotificationWindow] confirm_reply action failed:', error);
    } finally {
      closeNotification();
    }
  }, [closeNotification, getMissionId, notif?.id]);

  const handleConfirmReplyAction = useCallback((action: 'approve' | 'cancel' | 'edit') => {
    if (action === 'edit') {
      const initialText = typeof notif?.payload?.draft_text === 'string' ? notif.payload.draft_text : '';
      setManualReplyText(initialText);
      setEditingReply(true);
      invoke('set_notification_window_focusable', { focusable: true }).catch(() => {});
      return;
    }

    submitConfirmReply(action === 'approve').catch(() => {});
  }, [notif?.payload, submitConfirmReply]);

  const handleManualReplySubmit = useCallback((e: React.FormEvent) => {
    e.preventDefault();
    const text = manualReplyText.trim();
    if (!text) return;

    submitConfirmReply(true, text).catch(() => {});
  }, [manualReplyText, submitConfirmReply]);

  if (!notif) return null;

  const icon = KIND_ICON[notif.kind || ''] || '🔔';
  const isConfirmReply = notif.kind === 'confirm_reply';
  const draftText = typeof notif.payload?.draft_text === 'string' ? notif.payload.draft_text : '';
  const contactLabel = typeof notif.payload?.contact_label === 'string' ? notif.payload.contact_label : '';
  const dismissDurationMs = notif.config?.persistent ? 0 : (notif.config?.duration_ms ?? AUTO_DISMISS_MS);
  const timerKey = `${notif.id ?? notif.created_at_unix ?? notif.title ?? 'notification'}-${dismissDurationMs}`;

  return (
    <div
      ref={notificationRef}
      className={`macos-notification ${visible ? 'visible' : 'hidden'}${dismissPaused ? ' timer-paused' : ''}`}
      role="alert"
      onMouseEnter={pauseDismissTimer}
      onMouseLeave={resumeDismissTimer}
    >
      <button
        className="macos-notification-close"
        type="button"
        onPointerDown={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onClick={handleClose}
        aria-label="Fechar notificação"
      >
        ×
      </button>
      <div className="macos-notification-icon">
        <div className="icon-bubble">{icon}</div>
        {dismissDurationMs > 0 && (
          <div
            key={timerKey}
            className="notification-countdown"
            aria-hidden="true"
            style={{ '--notification-progress': dismissProgress } as React.CSSProperties}
          >
            <div className="notification-countdown-fill" />
          </div>
        )}
      </div>
      <div className="macos-notification-content">
        <div className="macos-notification-header">
          <span className="macos-notification-app">Neo Assistant</span>
          <span className="macos-notification-time">agora</span>
        </div>
        <div className="macos-notification-title">{notif.title || 'Nova notificação'}</div>
        {isConfirmReply ? (
          <div className="confirm-reply">
            {!editingReply ? (
              <>
                {contactLabel && <div className="confirm-reply-recipient">Para {contactLabel}</div>}
                <div className="confirm-reply-preview">{draftText || notif.description}</div>
                <div className="confirm-reply-actions">
                  <button
                    type="button"
                    className="confirm-reply-btn approve"
                    onClick={() => handleConfirmReplyAction('approve')}
                  >
                    Aprovo
                  </button>
                  <button
                    type="button"
                    className="confirm-reply-btn edit"
                    onClick={() => handleConfirmReplyAction('edit')}
                  >
                    Eu escrevo
                  </button>
                  <button
                    type="button"
                    className="confirm-reply-btn cancel"
                    onClick={() => handleConfirmReplyAction('cancel')}
                  >
                    Não fazer nada
                  </button>
                </div>
              </>
            ) : (
              <form className="confirm-reply-input-row" onSubmit={handleManualReplySubmit}>
                <input
                  ref={manualReplyInputRef}
                  className="confirm-reply-input"
                  value={manualReplyText}
                  onChange={(e) => setManualReplyText(e.target.value)}
                  placeholder="Responder ao Neo..."
                />
                <button
                  type="submit"
                  className="confirm-reply-send"
                  aria-label="Enviar resposta editada"
                  disabled={!manualReplyText.trim()}
                >
                  ↑
                </button>
              </form>
            )}
          </div>
        ) : notif.description && (
          <div className="macos-notification-body">{notif.description}</div>
        )}
        {!isConfirmReply && notif.fields && notif.fields.length > 0 && (
          <div className={`macos-notification-fields${notif.fields.length === 1 ? ' single-field' : ''}`}>
            {notif.fields.map((field, i) => (
              <div key={i} className="macos-notification-field">
                {field.label && <span className="field-label">{field.label}</span>}
                {field.field_type === 'select' ? (
                  <select
                    className="field-input field-select"
                    data-source={field.source}
                    defaultValue={field.value || ''}
                  >
                    <option value="" disabled>Selecione...</option>
                    {(field.options || '').split(',').filter(o => o.trim()).map(opt => (
                      <option key={opt} value={opt.trim()}>{opt.trim()}</option>
                    ))}
                  </select>
                ) : field.field_type === 'text' ? (
                  <input
                    type="text"
                    className="field-input field-text"
                    data-source={field.source}
                    placeholder={field.placeholder || field.value || field.label || ''}
                    defaultValue={field.input_value || ''}
                  />
                ) : (
                  <span className="field-value">{field.value || field.label}</span>
                )}
              </div>
            ))}
          </div>
        )}
        {!isConfirmReply && notif.actions && notif.actions.length > 0 && (
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
