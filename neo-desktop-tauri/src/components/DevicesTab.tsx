import { useEffect, useState } from 'react';
import type { DeviceInfo } from '../types/electron';

interface Props {
  currentDeviceId?: string;
}

/**
 * Cartão de pareamento — aparece enquanto este aparelho não tem token.
 *
 * Sem pareamento o canal de controle não sobe, e nada de "abaixa o volume do
 * PC da sala" chega aqui. O código sai do neo-panel (que tem credencial); este
 * aparelho não pode se auto-autorizar, senão a autenticação não valeria nada.
 */
function PairingCard({ onPaired }: { onPaired: () => void }) {
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (code.trim().length !== 6) {
      setError('O código tem 6 dígitos.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await window.electronAPI?.pairDevice(code.trim(), name.trim() || undefined);
      onPaired();
    } catch (err: any) {
      setError(typeof err === 'string' ? err : err?.message || 'Falha ao parear');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="device-card pairing-card">
      <div className="device-info">
        <div className="device-header">
          <span className="device-type">Parear este dispositivo</span>
        </div>
        <div className="device-meta">
          Gere um código no neo-panel e digite aqui. Sem isso o Neo não consegue
          controlar volume, mídia ou energia desta máquina.
        </div>
        <div className="pairing-fields">
          <input
            className="pairing-input"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            placeholder="000000"
            inputMode="numeric"
            maxLength={6}
            disabled={busy}
          />
          <input
            className="pairing-input pairing-input-name"
            value={name}
            onChange={(e) => setName(e.target.value.slice(0, 64))}
            placeholder="Nome (ex: PC da sala)"
            disabled={busy}
          />
          <button onClick={submit} disabled={busy}>
            {busy ? 'Pareando…' : 'Parear'}
          </button>
        </div>
        {error && <div className="pairing-error">❌ {error}</div>}
      </div>
    </div>
  );
}

/**
 * Botão de remover, com confirmação em dois toques.
 *
 * Remover o PRÓPRIO aparelho equivale a desparear (o token vai junto), então
 * ele avisa disso antes — é uma ação bem diferente de tirar um notebook velho
 * da lista, e o botão é o mesmo.
 */
function RemoveButton({ device, onRemoved }: { device: DeviceInfo; onRemoved: (self: boolean) => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const remove = async () => {
    setBusy(true);
    try {
      const wasSelf = await window.electronAPI?.removeDevice(device.device_id);
      onRemoved(Boolean(wasSelf));
    } catch {
      setBusy(false);
      setConfirming(false);
    }
  };

  if (!confirming) {
    return (
      <button
        className="device-remove"
        title="Remover este dispositivo da conta"
        onClick={() => setConfirming(true)}
      >
        Remover
      </button>
    );
  }

  return (
    <div className="device-remove-confirm">
      <span>
        {device.is_current_device
          ? 'Remover ESTE aparelho? Ele será despareado.'
          : `Remover "${device.name || device.model}"?`}
      </span>
      <button className="device-remove danger" onClick={remove} disabled={busy}>
        {busy ? '…' : 'Confirmar'}
      </button>
      <button className="device-remove" onClick={() => setConfirming(false)} disabled={busy}>
        Cancelar
      </button>
    </div>
  );
}

/** Nome editável no lugar — é o nome que o usuário vai FALAR com o Neo. */
function DeviceName({ device, onRenamed }: { device: DeviceInfo; onRenamed: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(device.display_name || '');

  const save = async () => {
    setEditing(false);
    const next = value.trim();
    if (next === (device.display_name || '')) return;
    try {
      await window.electronAPI?.renameDevice(device.device_id, next || null);
      onRenamed();
    } catch {
      /* a lista recarrega sozinha; um erro aqui não vale um alerta */
    }
  };

  if (editing) {
    return (
      <input
        className="device-name-input"
        value={value}
        autoFocus
        maxLength={64}
        onChange={(e) => setValue(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === 'Enter') save();
          if (e.key === 'Escape') setEditing(false);
        }}
      />
    );
  }

  return (
    <span
      className="device-name"
      title="Clique para renomear — é por este nome que você pede as coisas ao Neo"
      onClick={() => setEditing(true)}
    >
      {device.name || device.model}
    </span>
  );
}

/**
 * Credencial da ponte da extensão de música.
 *
 * A ponte (`ws://127.0.0.1:17872`) passou a exigir token no handshake. Loopback
 * não é fronteira de confiança: qualquer processo da máquina alcança, e uma
 * PÁGINA WEB abre WebSocket para 127.0.0.1 — e como a ponte guarda só uma
 * conexão, a hostil deslocava a extensão de verdade. Ver
 * neo-core/docs/api-auth-plan.md §11.
 *
 * Mostrar esta credencial na tela é o ponto dela: existe para ser copiada para
 * outro programa. O `device-token` continua sem aparecer em lugar nenhum.
 */
function ExtBridgeTokenCard() {
  const [token, setToken] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [revelado, setRevelado] = useState(false);
  const [copiado, setCopiado] = useState(false);

  useEffect(() => {
    window.electronAPI
      ?.extBridgeToken()
      .then(setToken)
      .catch((e: unknown) =>
        setErro(typeof e === 'string' ? e : (e as Error)?.message || 'falhou'),
      );
  }, []);

  const copiar = async () => {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      setCopiado(true);
      setTimeout(() => setCopiado(false), 1800);
    } catch {
      // Sem clipboard (permissão do webview): revelar já resolve, dá pra
      // selecionar com o mouse.
      setRevelado(true);
    }
  };

  return (
    <div className="device-card ext-token-card">
      <div className="device-info">
        <div className="device-header">
          <span className="device-type">Extensão de música</span>
        </div>
        <div className="device-meta">
          Cole este token no popup da extensão (Neo Music Bridge) uma vez. Sem ele
          a ponte recusa a conexão — e é isso que impede uma página web qualquer de
          se passar pela extensão.
        </div>
        {erro ? (
          <div className="device-meta ext-token-error">Não consegui ler o token: {erro}</div>
        ) : (
          <div className="ext-token-row">
            <code className="ext-token-value">
              {token ? (revelado ? token : `${token.slice(0, 6)}${'•'.repeat(18)}`) : 'carregando…'}
            </code>
            <button className="device-remove" onClick={() => setRevelado((v) => !v)} disabled={!token}>
              {revelado ? 'Ocultar' : 'Mostrar'}
            </button>
            <button className="device-remove" onClick={copiar} disabled={!token}>
              {copiado ? 'Copiado ✓' : 'Copiar'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Como o tipo de device aparece na lista.
 *
 * Era `device_type === 'neo-mobile' ? 'Neo Mobile' : 'Neo Desktop'` — ou seja,
 * TODO o resto mentia chamando-se de desktop. Com superfície de navegador
 * (neo-core/docs/browser-surface-plan.md) isso deixa de ser cosmético: esta é a
 * tela onde você revoga uma credencial, e revogar a linha errada tira o Neo do ar
 * na máquina em vez de tirar a extensão.
 */
function tipoLegivel(tipo: string | undefined): string {
  switch (tipo) {
    case 'neo-mobile':
      return 'Neo Mobile';
    case 'neo-browser-ext':
      return 'Navegador';
    case 'neo-desktop':
    case 'neo-desktop-tauri':
      return 'Neo Desktop';
    default:
      return tipo || 'Dispositivo';
  }
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
  const [paired, setPaired] = useState<boolean | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const fetchDevices = async () => {
      try {
        setError(null);
        const status = await window.electronAPI?.devicePairingStatus();
        if (cancelled) return;
        setPaired(status?.paired ?? false);

        const result = await window.electronAPI?.listConnectedDevices();
        if (cancelled) return;
        setDevices(result || []);
      } catch (err: any) {
        // O Tauri rejeita `invoke` com uma STRING, não com Error — então
        // `err.message` era sempre undefined e TODA falha virava o genérico
        // "Falha ao carregar dispositivos". O Rust já produz a causa exata
        // ("… respondeu 401", "não alcancei o neo-core: …") e a UI a jogava fora,
        // transformando "o túnel caiu" e "o token não vale mais" na mesma tela.
        if (!cancelled) {
          const motivo =
            typeof err === 'string' ? err : err?.message || 'Falha ao carregar dispositivos';
          setError(motivo);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchDevices();
    const interval = setInterval(fetchDevices, 10000); // refresh every 10s
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [reloadKey]);

  const reload = () => setReloadKey((k) => k + 1);

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

  if (paired === false) {
    return (
      <div className="devices-list">
        <PairingCard onPaired={reload} />
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

  // Navegador pareado aparece ANINHADO na máquina dele, não como aparelho solto.
  // Não é enfeite: esta é a tela onde se revoga credencial, e um navegador solto
  // ao lado do computador convida a revogar a linha errada — o que tira o Neo do
  // ar na máquina em vez de tirar a extensão.
  // Ver neo-core/docs/browser-surface-plan.md §12.2 (item 6).
  const superficiesPorHost = new Map<string, DeviceInfo[]>();
  for (const d of devices) {
    if (d.host_device_id) {
      const lista = superficiesPorHost.get(d.host_device_id) ?? [];
      lista.push(d);
      superficiesPorHost.set(d.host_device_id, lista);
    }
  }
  // Superfície órfã (hospedeiro removido, ou pareada antes do vínculo existir)
  // continua na lista de cima: esconder um aparelho que EXISTE é pior que
  // mostrá-lo fora de lugar.
  const hostsPresentes = new Set(devices.map((d) => d.device_id));
  const raiz = devices.filter(
    (d) => !d.host_device_id || !hostsPresentes.has(d.host_device_id),
  );

  return (
    <div className="devices-list">
      <ExtBridgeTokenCard />
      {raiz.map((device) => (
        <div
          key={device.device_id}
          className={`device-card ${device.is_current_device ? 'current' : ''}`}
        >
          <div className="device-icon">
            <DeviceIcon type={device.device_type} />
          </div>
          <div className="device-info">
            <div className="device-header">
              <span className="device-type">{tipoLegivel(device.device_type)}</span>
              {device.is_current_device && (
                <span className="device-badge">este dispositivo</span>
              )}
              {/* Controlável = canal de controle aberto. Mais estrito que
                  "online": é o que decide se o Neo consegue mexer no volume. */}
              {device.controllable && (
                <span className="device-badge device-badge-control">controlável</span>
              )}
            </div>
            <div className="device-model">
              <DeviceName device={device} onRenamed={reload} />
            </div>
            <div className="device-meta">
              <span>{device.model}</span>
              <span>{device.os_platform} {device.os_version}</span>
              {device.client_version && <span>v{device.client_version}</span>}
            </div>
            <div className="device-time">
              Conectado {formatTimestamp(device.connected_at_unix)}
            </div>
            <RemoveButton
              device={device}
              onRemoved={(wasSelf) => {
                // Removeu a si mesmo = despareou: o cartão de pareamento volta.
                if (wasSelf) setPaired(false);
                reload();
              }}
            />

            {/* Os navegadores desta máquina. Revogar um deles não toca no
                computador — são linhas separadas no registro. */}
            {(superficiesPorHost.get(device.device_id) ?? []).map((surface) => (
              <div key={surface.device_id} className="device-surface">
                <div className="device-surface-head">
                  <span className="device-surface-tipo">
                    {tipoLegivel(surface.device_type)}
                  </span>
                  <DeviceName device={surface} onRenamed={reload} />
                  {surface.controllable && (
                    <span className="device-badge device-badge-control">conectado</span>
                  )}
                </div>
                <div className="device-meta">
                  <span>{surface.model}</span>
                  {surface.client_version && <span>v{surface.client_version}</span>}
                </div>
                <RemoveButton device={surface} onRemoved={() => reload()} />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
};