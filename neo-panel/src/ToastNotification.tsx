import { useEffect, useState, useRef, useCallback } from 'react';

export interface ToastData {
  id: string;
  title: string;
  message: string;
  type?: 'info' | 'success' | 'warning' | 'error';
  duration?: number;
  icon?: string;
}

interface ToastNotificationProps {
  toast: ToastData;
  onClose: (id: string) => void;
}

const defaultDuration = 5000;

const ToastNotification = ({ toast, onClose }: ToastNotificationProps) => {
  const [isVisible, setIsVisible] = useState(false);
  const [isExiting, setIsExiting] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const duration = toast.duration ?? defaultDuration;

  useEffect(() => {
    // Trigger entrance animation
    requestAnimationFrame(() => setIsVisible(true));

    // Auto-dismiss timer
    timerRef.current = setTimeout(() => {
      setIsExiting(true);
      setTimeout(() => onClose(toast.id), 300);
    }, duration);

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [toast.id, duration, onClose]);

  const handleClose = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setIsExiting(true);
    setTimeout(() => onClose(toast.id), 300);
  }, [toast.id, onClose]);

  const getTypeStyles = () => {
    switch (toast.type) {
      case 'success':
        return {
          bg: 'linear-gradient(135deg, rgba(46, 125, 50, 0.95), rgba(56, 142, 60, 0.95))',
          border: '#2e7d32',
          icon: '✓'
        };
      case 'warning':
        return {
          bg: 'linear-gradient(135deg, rgba(245, 124, 0, 0.95), rgba(255, 152, 0, 0.95))',
          border: '#f57c00',
          icon: '⚠'
        };
      case 'error':
        return {
          bg: 'linear-gradient(135deg, rgba(198, 40, 40, 0.95), rgba(244, 67, 54, 0.95))',
          border: '#d32f2f',
          icon: '✕'
        };
      default:
        return {
          bg: 'linear-gradient(135deg, rgba(44, 62, 80, 0.95), rgba(52, 73, 94, 0.95))',
          border: '#3498db',
          icon: 'ℹ'
        };
    }
  };

  const styles = getTypeStyles();

  return (
    <div
      style={{
        position: 'relative',
        width: 320,
        marginBottom: 8,
        opacity: isVisible && !isExiting ? 1 : 0,
        transform: isVisible && !isExiting 
          ? 'translateX(0) translateY(0)' 
          : 'translateX(100%) translateY(-20px)',
        transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
      }}
    >
      {/* macOS-style notification container */}
      <div
        style={{
          background: styles.bg,
          borderRadius: 12,
          border: `1px solid ${styles.border}`,
          boxShadow: '0 10px 40px rgba(0, 0, 0, 0.3), 0 2px 8px rgba(0, 0, 0, 0.2)',
          backdropFilter: 'blur(20px)',
          overflow: 'hidden',
        }}
      >
        {/* Left accent bar */}
        <div
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            bottom: 0,
            width: 4,
            background: styles.border,
            borderRadius: '12px 0 0 12px',
          }}
        />

        {/* Content */}
        <div style={{ padding: '14px 16px 14px 20px' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start' }}>
            {/* Icon */}
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: '50%',
                background: 'rgba(255, 255, 255, 0.15)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontSize: 14,
                fontWeight: 600,
                marginRight: 12,
                flexShrink: 0,
              }}
            >
              {toast.icon || styles.icon}
            </div>

            {/* Text content */}
            <div style={{ flex: 1, minWidth: 0 }}>
              {toast.title && (
                <div
                  style={{
                    color: '#fff',
                    fontSize: 13,
                    fontWeight: 600,
                    marginBottom: 4,
                    lineHeight: 1.3,
                  }}
                >
                  {toast.title}
                </div>
              )}
              <div
                style={{
                  color: 'rgba(255, 255, 255, 0.9)',
                  fontSize: 12,
                  lineHeight: 1.4,
                  wordWrap: 'break-word',
                  overflow: 'hidden',
                  display: '-webkit-box',
                  WebkitLineClamp: 3,
                  WebkitBoxOrient: 'vertical',
                }}
              >
                {toast.message}
              </div>
            </div>

            {/* Close button */}
            <button
              onClick={handleClose}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'rgba(255, 255, 255, 0.6)',
                cursor: 'pointer',
                padding: 4,
                marginLeft: 8,
                fontSize: 16,
                lineHeight: 1,
                borderRadius: 4,
                transition: 'all 0.15s ease',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.1)';
                e.currentTarget.style.color = '#fff';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.color = 'rgba(255, 255, 255, 0.6)';
              }}
            >
              ✕
            </button>
          </div>
        </div>

        {/* Progress bar */}
        <div
          style={{
            height: 3,
            background: 'rgba(255, 255, 255, 0.2)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              height: '100%',
              background: 'rgba(255, 255, 255, 0.6)',
              animation: `progress ${duration}ms linear`,
              transformOrigin: 'left',
            }}
          />
        </div>

        <style>
          {`
            @keyframes progress {
              from { transform: scaleX(1); }
              to { transform: scaleX(0); }
            }
          `}
        </style>
      </div>
    </div>
  );
};

// Toast Container Component
interface ToastContainerProps {
  toasts: ToastData[];
  onRemove: (id: string) => void;
  position?: 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left';
}

export const ToastContainer = ({ 
  toasts, 
  onRemove, 
  position = 'bottom-right' 
}: ToastContainerProps) => {
  const positions = {
    'bottom-right': { bottom: 20, right: 20 },
    'bottom-left': { bottom: 20, left: 20 },
    'top-right': { top: 20, right: 20 },
    'top-left': { top: 20, left: 20 },
  };

  const pos = positions[position];
  const isBottom = position.includes('bottom');

  return (
    <div
      style={{
        position: 'fixed',
        zIndex: 99999,
        display: 'flex',
        flexDirection: isBottom ? 'column' : 'column-reverse',
        gap: 0,
        ...pos,
      }}
    >
      {toasts.map((toast) => (
        <ToastNotification
          key={toast.id}
          toast={toast}
          onClose={onRemove}
        />
      ))}
    </div>
  );
};

// Hook for managing toasts
export function useToast() {
  const [toasts, setToasts] = useState<ToastData[]>([]);

  const addToast = useCallback((toast: Omit<ToastData, 'id'>) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    setToasts((prev) => [...prev, { ...toast, id }]);
    return id;
  }, []);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const clearAll = useCallback(() => {
    setToasts([]);
  }, []);

  return {
    toasts,
    addToast,
    removeToast,
    clearAll,
  };
}

export default ToastNotification;