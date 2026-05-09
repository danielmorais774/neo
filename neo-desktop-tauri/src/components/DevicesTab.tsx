import { useEffect, useState } from 'react';
import type { DeviceInfo } from '../types/electron';

interface Props {
  currentDeviceId?: string;
}

function formatTimestamp(unix: number): string {
  if (!unix) return '—';
  const d = new Date(unix * 1000);
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 60000) return 'agora';
  if (diff < 3600000) return `${Math.floor(diff / 60000)}min atrás`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h atrás`;
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function DeviceIcon({ type }: { type: string }) {
  if (type === 'neo-mobile') {
    return (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="5" y="2" width="14" height="20" rx="2" ry="2"/>
        <line x1="12" y1="18" x2="12.01" y2="18"/>
      </svg>
    );
  }
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
      <line x1="8" y1="21" x2="16" y2="21"/>
      <line x1="12" y1="17" x2="12" y2="21"/>
    </svg>
  );
}

export const DevicesTab = ({ currentDeviceId }: Props) => {
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchDevices = async () => {
      try {
        setLoading(true);
        setError(null);
        const result = await window.electronAPI?.listConnectedDevices();
        setDevices(result || []);
      } catch (err: any) {
        setError(err?.message || 'Falha ao carregar dispositivos');
      } finally {
        setLoading(false);
      }
    };

    fetchDevices();
    const interval = setInterval(fetchDevices, 10000); // refresh every 10s
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div className="devices-loading">
        <div className="spinner" />
        <span>Carregando dispositivos...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="devices-error">
        <span>❌ {error}</span>
        <button onClick={() => setDevices([])}>Tentar novamente</button>
      </div>
    );
  }

  if (devices.length === 0) {
    return (
      <div className="devices-empty">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
          <line x1="8" y1="21" x2="16" y2="21"/>
          <line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
        <p>Nenhum dispositivo conectado</p>
        <span>Dispositivos que usam o neo-core aparecerão aqui</span>
      </div>
    );
  }

  return (
    <div className="devices-list">
      {devices.map((device) => (
        <div
          key={device.device_id}
          className={`device-card ${device.is_current_device ? 'current' : ''}`}
        >
          <div className="device-icon">
            <DeviceIcon type={device.device_type} />
          </div>
          <div className="device-info">
            <div className="device-header">
              <span className="device-type">
                {device.device_type === 'neo-mobile' ? 'Neo Mobile' : 'Neo Desktop'}
              </span>
              {device.is_current_device && (
                <span className="device-badge">este dispositivo</span>
              )}
            </div>
            <div className="device-model">{device.model}</div>
            <div className="device-meta">
              <span>{device.os_platform} {device.os_version}</span>
              {device.client_version && <span>v{device.client_version}</span>}
            </div>
            <div className="device-time">
              Conectado {formatTimestamp(device.connected_at_unix)}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
};