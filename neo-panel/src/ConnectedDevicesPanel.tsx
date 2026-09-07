import { useEffect, useState } from 'react'

interface DeviceInfo {
  device_id: string;
  user_id?: string;
  name?: string;
  display_name?: string | null;
  controllable?: boolean;
  capabilities?: Record<string, boolean>;
  device_type: string;
  model: string;
  hardware_id: string;
  os_platform: string;
  os_version: string;
  client_version: string;
  ip_address: string;
  connected_at_unix: number;
  is_current_device: boolean;
  is_online: boolean;
  ws_connection_id: string | null;
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

const NOTIFICATION_KINDS = [
  { value: 'approval_required', label: '🤖 Approval Required' },
  { value: 'mission_started', label: '⚡ Mission Started' },
  { value: 'mission_update', label: '💬 Mission Update' },
  { value: 'mission_completed', label: '✨ Mission Completed' },
  { value: 'mission_failed', label: '❌ Mission Failed' },
  { value: 'unsolicited_message', label: '📩 Unsolicited Message' },
];

const ACTION_TYPES = [
  { value: 'reply', label: 'Reply (interativo)' },
  { value: 'dismiss', label: 'Dismiss (fechar)' },
];

const BUTTON_STYLES = [
  { value: 'contained', label: 'Contained' },
  { value: 'outlined', label: 'Outlined' },
  { value: 'text', label: 'Text' },
  { value: 'ghost', label: 'Ghost' },
];

const FIELD_TYPES = [
  { value: 'text', label: 'Text' },
  { value: 'select', label: 'Select' },
];

interface CustomField {
  label: string;
  value: string;
  source: string;
  placeholder?: string;
  fieldType: 'text' | 'select';
  options?: string;
}

interface ActionConfig {
  label: string;
  actionType: 'reply' | 'dismiss';
  buttonStyle: 'contained' | 'outlined' | 'text' | 'ghost';
}

function DeviceModal({ device, onClose }: { device: DeviceInfo; onClose: () => void }) {
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [notifTitle, setNotifTitle] = useState('Teste de Notificação');
  const [notifDescription, setNotifDescription] = useState('');
  const [notifKind, setNotifKind] = useState('approval_required');
  const [customFields, setCustomFields] = useState<CustomField[]>([]);
  const [actions, setActions] = useState<ActionConfig[]>([
    { label: 'Responder', actionType: 'reply', buttonStyle: 'contained' },
    { label: 'Dispensar', actionType: 'dismiss', buttonStyle: 'outlined' },
  ]);
  const [configDuration, setConfigDuration] = useState(0);
  const [configPosition, setConfigPosition] = useState('bottom_right');
  const [configPersistent, setConfigPersistent] = useState(true);
  const [configPlaySound, setConfigPlaySound] = useState(true);
  const [notifSource, setNotifSource] = useState('neo-panel-device-modal');

  const addField = () => setCustomFields(prev => [...prev, { label: '', value: '', source: '', placeholder: '', fieldType: 'text' }]);
  const removeField = (index: number) => setCustomFields(prev => prev.filter((_, i) => i !== index));
  const updateField = (index: number, key: keyof CustomField, val: string) => {
    setCustomFields(prev => prev.map((f, i) => i === index ? { ...f, [key]: val } : f));
  };
  const addAction = () => setActions(prev => [...prev, { label: '', actionType: 'dismiss', buttonStyle: 'outlined' }]);
  const removeAction = (index: number) => setActions(prev => prev.filter((_, i) => i !== index));
  const updateAction = (index: number, key: keyof ActionConfig, val: string) => {
    setActions(prev => prev.map((a, i) => i === index ? { ...a, [key]: val } : a));
  };

  const sendTestNotification = async () => {
    setSending(true);
    setResult(null);
    try {
      const payload = {
        id: `test-${Date.now()}`,
        user_id: 'neo-panel-test',
        mission_id: null,
        kind: notifKind,
        title: notifTitle || 'Teste de Notificação',
        description: notifDescription || `Teste enviado do neo-panel para ${device.model || device.device_type}`,
        actions: actions.filter(a => a.label.trim()).map(a => ({
          action_type: a.actionType,
          label: a.label.trim(),
          button_style: a.buttonStyle,
          payload: { source: notifSource },
        })),
        payload: { source: notifSource, device_id: device.device_id },
        created_at_unix: Math.floor(Date.now() / 1000),
        config: {
          duration_ms: configDuration,
          position: configPosition,
          persistent: configPersistent,
          play_sound: configPlaySound,
        },
        device_id: device.device_id,
        fields: customFields.filter(f => f.label.trim() || f.value.trim() || f.source.trim()).map(f => ({
          label: f.label,
          value: f.value,
          source: f.source,
          placeholder: f.placeholder,
          field_type: f.fieldType,
          options: f.fieldType === 'select' ? f.options : undefined,
        })),
      };

      const response = await fetch('http://127.0.0.1:8000/api/v1/debug/test-notification', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (response.ok) {
        setResult({ ok: true, text: 'Notificação enviada com sucesso!' });
      } else {
        setResult({ ok: false, text: `Erro: ${data.detail || response.statusText}` });
      }
    } catch (err) {
      setResult({ ok: false, text: `Erro de conexão: ${err}` });
    } finally {
      setSending(false);
    }
  };

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
    }} onClick={onClose}>
      <div style={{
        background: '#1e2235', border: '1px solid #334155', borderRadius: 16,
        padding: 24, width: 680, maxWidth: '95vw', color: '#e2e8f0'
      }} onClick={e => e.stopPropagation()}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
          <div style={{
            width: 48, height: 48, borderRadius: 12,
            background: 'rgba(124, 58, 237, 0.2)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a78bfa'
          }}>
            <DeviceIcon type={device.device_type} />
          </div>
          <div>
            <h3 style={{ margin: 0, fontSize: 16 }}>
              {device.device_type === 'neo-mobile' ? 'Neo Mobile' : 'Neo Desktop'}
            </h3>
            <p style={{ margin: 0, fontSize: 12, color: '#64748b' }}>{device.model}</p>
          </div>
          <button onClick={onClose} style={{
            marginLeft: 'auto', background: 'transparent', border: 'none',
            color: '#64748b', cursor: 'pointer', fontSize: 20
          }}>×</button>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 20 }}>
          <InfoRow label="Device ID" value={device.device_id} mono />
          <InfoRow label="Hardware ID" value={device.hardware_id || '-'} mono />
          <InfoRow label="Platform" value={`${device.os_platform} ${device.os_version}`} />
          <InfoRow label="Client" value={device.client_version || '-'} />
          <InfoRow label="IP" value={device.ip_address || '-'} />
          <InfoRow label="Online" value={device.is_online ? 'Sim' : 'Não'} />
          <InfoRow label="WS Connection" value={device.ws_connection_id || '-'} mono />
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            TÍTULO DA NOTIFICAÇÃO
          </label>
          <input
            value={notifTitle}
            onChange={e => setNotifTitle(e.target.value)}
            placeholder="Teste de Notificação"
            style={{
              width: '100%', background: '#13161f', border: '1px solid #1e2235',
              borderRadius: 8, padding: '8px 12px', color: '#e2e8f0', fontSize: 13,
              outline: 'none', boxSizing: 'border-box'
            }}
          />
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            DESCRIÇÃO
          </label>
          <textarea
            value={notifDescription}
            onChange={e => setNotifDescription(e.target.value)}
            placeholder="Descrição opcional..."
            rows={2}
            style={{
              width: '100%', background: '#13161f', border: '1px solid #1e2235',
              borderRadius: 8, padding: '8px 12px', color: '#e2e8f0', fontSize: 13,
              outline: 'none', resize: 'none', fontFamily: 'inherit', boxSizing: 'border-box'
            }}
          />
        </div>

        
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            TIPO DE NOTIFICAÇÃO
          </label>
          <select
            value={notifKind}
            onChange={e => setNotifKind(e.target.value)}
            style={{
              width: '100%', background: '#13161f', border: '1px solid #1e2235',
              borderRadius: 8, padding: '8px 12px', color: '#e2e8f0', fontSize: 13,
              outline: 'none', boxSizing: 'border-box', cursor: 'pointer'
            }}
          >
            {NOTIFICATION_KINDS.map(k => (
              <option key={k.value} value={k.value}>{k.label}</option>
            ))}
          </select>
        </div>

        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            AÇÕES
          </label>
          {actions.map((action, i) => (
            <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6, alignItems: 'center' }}>
              <input
                value={action.label}
                onChange={e => updateAction(i, 'label', e.target.value)}
                placeholder="Label"
                style={{
                  flex: 2, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12,
                  outline: 'none', boxSizing: 'border-box'
                }}
              />
              <select
                value={action.actionType}
                onChange={e => updateAction(i, 'actionType', e.target.value)}
                style={{
                  flex: 1, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 6px', color: '#e2e8f0', fontSize: 11,
                  outline: 'none', boxSizing: 'border-box', cursor: 'pointer'
                }}
              >
                {ACTION_TYPES.map(a => (
                  <option key={a.value} value={a.value}>{a.label}</option>
                ))}
              </select>
              <select
                value={action.buttonStyle}
                onChange={e => updateAction(i, 'buttonStyle', e.target.value)}
                style={{
                  flex: 1, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 6px', color: '#e2e8f0', fontSize: 11,
                  outline: 'none', boxSizing: 'border-box', cursor: 'pointer'
                }}
              >
                {BUTTON_STYLES.map(b => (
                  <option key={b.value} value={b.value}>{b.label}</option>
                ))}
              </select>
              <button onClick={() => removeAction(i)} style={{
                background: 'transparent', border: '1px solid #334155',
                borderRadius: 6, padding: '6px 10px', color: '#64748b',
                cursor: 'pointer', fontSize: 14, lineHeight: 1
              }}>×</button>
            </div>
          ))}
          <button onClick={addAction} style={{
            background: 'transparent', border: '1px dashed #334155',
            borderRadius: 6, padding: '6px 10px', color: '#64748b',
            cursor: 'pointer', fontSize: 12, width: '100%', marginTop: 4
          }}>
            + Adicionar Ação
          </button>
        </div>

        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            FIELDS (label/value customizado)
          </label>
          {customFields.map((field, i) => (
            <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6, alignItems: 'center' }}>
              <input
                value={field.source}
                onChange={e => updateField(i, 'source', e.target.value)}
                placeholder="Source"
                style={{
                  width: 80, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 4px', color: '#e2e8f0', fontSize: 11,
                  outline: 'none', boxSizing: 'border-box'
                }}
              />
              <select
                value={field.fieldType}
                onChange={e => updateField(i, 'fieldType', e.target.value)}
                style={{
                  width: 70, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 4px', color: '#e2e8f0', fontSize: 11,
                  outline: 'none', boxSizing: 'border-box', cursor: 'pointer'
                }}
              >
                {FIELD_TYPES.map(f => (
                  <option key={f.value} value={f.value}>{f.label}</option>
                ))}
              </select>
              <input
                value={field.placeholder || ''}
                onChange={e => updateField(i, 'placeholder', e.target.value)}
                placeholder="Placeholder"
                style={{
                  width: 80, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 4px', color: '#e2e8f0', fontSize: 11,
                  outline: 'none', boxSizing: 'border-box'
                }}
              />
              <input
                value={field.label}
                onChange={e => updateField(i, 'label', e.target.value)}
                placeholder="Label"
                style={{
                  flex: 1, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12,
                  outline: 'none', boxSizing: 'border-box'
                }}
              />
              <input
                value={field.value}
                onChange={e => updateField(i, 'value', e.target.value)}
                placeholder="Value"
                style={{
                  flex: 1, background: '#13161f', border: '1px solid #1e2235',
                  borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12,
                  outline: 'none', boxSizing: 'border-box'
                }}
              />
              {field.fieldType === 'select' && (
                <input
                  value={field.options || ''}
                  onChange={e => updateField(i, 'options', e.target.value)}
                  placeholder="options: a,b,c"
                  style={{
                    flex: 1, background: '#13161f', border: '1px solid #1e2235',
                    borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12,
                    outline: 'none', boxSizing: 'border-box'
                  }}
                />
              )}
              <button onClick={() => removeField(i)} style={{
                background: 'transparent', border: '1px solid #334155',
                borderRadius: 6, padding: '6px 10px', color: '#64748b',
                cursor: 'pointer', fontSize: 14, lineHeight: 1
              }}>×</button>
            </div>
          ))}
          <button onClick={addField} style={{
            background: 'transparent', border: '1px dashed #334155',
            borderRadius: 6, padding: '6px 10px', color: '#64748b',
            cursor: 'pointer', fontSize: 12, width: '100%', marginTop: 4
          }}>
            + Adicionar Field
          </button>
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            CONFIGURAÇÕES
          </label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ display: 'flex', gap: 8 }}>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: 10, color: '#475569', display: 'block', marginBottom: 4 }}>Duração (ms)</label>
                <input
                  type="number"
                  value={configDuration}
                  onChange={e => setConfigDuration(Number(e.target.value))}
                  placeholder="0 = infinito"
                  style={{
                    width: '100%', background: '#13161f', border: '1px solid #1e2235',
                    borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12,
                    outline: 'none', boxSizing: 'border-box'
                  }}
                />
              </div>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: 10, color: '#475569', display: 'block', marginBottom: 4 }}>Posição</label>
                <select
                  value={configPosition}
                  onChange={e => setConfigPosition(e.target.value)}
                  style={{
                    width: '100%', background: '#13161f', border: '1px solid #1e2235',
                    borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12,
                    outline: 'none', boxSizing: 'border-box', cursor: 'pointer'
                  }}
                >
                  <option value="bottom_right">Bottom Right</option>
                  <option value="bottom_left">Bottom Left</option>
                  <option value="top_right">Top Right</option>
                  <option value="top_left">Top Left</option>
                  <option value="center">Center</option>
                </select>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 12 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={configPersistent}
                  onChange={e => setConfigPersistent(e.target.checked)}
                  style={{ cursor: 'pointer' }}
                />
                <span style={{ fontSize: 12, color: '#94a3b8' }}>Persistente</span>
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={configPlaySound}
                  onChange={e => setConfigPlaySound(e.target.checked)}
                  style={{ cursor: 'pointer' }}
                />
                <span style={{ fontSize: 12, color: '#94a3b8' }}>Som</span>
              </label>
            </div>
          </div>
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 11, color: '#64748b', fontWeight: 600, display: 'block', marginBottom: 6 }}>
            SOURCE
          </label>
          <input
            value={notifSource}
            onChange={e => setNotifSource(e.target.value)}
            placeholder="neo-panel-device-modal"
            style={{
              width: '100%', background: '#13161f', border: '1px solid #1e2235',
              borderRadius: 8, padding: '8px 12px', color: '#e2e8f0', fontSize: 13,
              outline: 'none', boxSizing: 'border-box'
            }}
          />
        </div>

        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <button
            onClick={() => {
              setNotifKind('approval_required');
              setNotifTitle('Confirmação Necessária');
              setNotifDescription('Clique em Confirmar para prosseguir');
              setActions([
                { label: '✅ Confirmar', actionType: 'reply', buttonStyle: 'contained' },
                { label: '❌ Cancelar', actionType: 'dismiss', buttonStyle: 'outlined' },
              ]);
            }}
            style={{
              flex: 1, padding: '8px 12px', borderRadius: 8,
              background: '#059669', color: '#fff', border: 'none',
              cursor: 'pointer', fontSize: 12, fontWeight: 600
            }}
          >
            ⚡ Notificação Confirm Reply
          </button>
          <button
            onClick={() => {
              setNotifKind('mission_started');
              setNotifTitle('Missao Iniciada');
              setNotifDescription('Uma nova missao foi iniciada com sucesso');
              setActions([
                { label: 'Ver Detalhes', actionType: 'reply', buttonStyle: 'outlined' },
              ]);
            }}
            style={{
              flex: 1, padding: '8px 12px', borderRadius: 8,
              background: '#7c3aed', color: '#fff', border: 'none',
              cursor: 'pointer', fontSize: 12, fontWeight: 600
            }}
          >
            🚀 Notificação Mission
          </button>
        </div>

        <button
          onClick={sendTestNotification}
          disabled={sending || !device.is_online || !device.ws_connection_id}
          style={{
            width: '100%', padding: '10px 16px', borderRadius: 8,
            background: (sending || !device.is_online || !device.ws_connection_id) ? '#334155' : '#7c3aed',
            color: '#fff', border: 'none', cursor: (sending || !device.is_online || !device.ws_connection_id) ? 'not-allowed' : 'pointer',
            fontSize: 13, fontWeight: 600
          }}
        >
          {sending ? '⏳ Enviando...' : '🔔 Enviar Notificação de Teste'}
        </button>

        {result && (
          <div style={{
            marginTop: 12, padding: '8px 12px', borderRadius: 8,
            background: result.ok ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)',
            color: result.ok ? '#4ade80' : '#f87171',
            fontSize: 12
          }}>
            {result.ok ? '✅' : '❌'} {result.text}
          </div>
        )}
      </div>
    </div>
  );
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', gap: 8, fontSize: 12 }}>
      <span style={{ color: '#64748b', minWidth: 90 }}>{label}</span>
      <span style={{
        color: '#e2e8f0',
        fontFamily: mono ? 'monospace' : 'inherit',
        fontSize: mono ? 10 : 12,
        wordBreak: 'break-all'
      }}>{value}</span>
    </div>
  );
}

/**
 * Gerador de código de pareamento.
 *
 * É daqui que sai a autoridade de dizer "este aparelho é do usuário X": o
 * painel tem credencial, o desktop não tem nenhuma (o user_id dele vem de uma
 * env var com fallback hardcoded). O código dura 5 minutos, vale uma vez só, e
 * gerar outro invalida o anterior.
 */
function PairingCodeCard({ devices }: { devices: DeviceInfo[] }) {
  // Em qual conta o aparelho vai cair. O default é o dono da maioria dos
  // devices já registrados — com mais de um usuário no banco, inferir sozinho
  // no servidor seria chutar, e parear na conta errada é silencioso.
  const suggestedUserId = (() => {
    const tally = new Map<string, number>();
    for (const d of devices) {
      if (d.user_id) tally.set(d.user_id, (tally.get(d.user_id) ?? 0) + 1);
    }
    let best = '';
    let bestCount = 0;
    for (const [id, count] of tally) {
      if (count > bestCount) {
        best = id;
        bestCount = count;
      }
    }
    return best;
  })();

  const [userId, setUserId] = useState(suggestedUserId);
  const [code, setCode] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<number>(0);
  const [remaining, setRemaining] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A lista chega depois do primeiro render; só adota a sugestão enquanto o
  // campo não foi mexido (vazio).
  useEffect(() => {
    setUserId((current) => current || suggestedUserId);
  }, [suggestedUserId]);

  useEffect(() => {
    if (!expiresAt) return;
    const tick = () => setRemaining(Math.max(0, Math.round((expiresAt - Date.now()) / 1000)));
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [expiresAt]);

  const generate = async () => {
    setBusy(true);
    setError(null);
    try {
      const query = userId.trim() ? `?user_id=${encodeURIComponent(userId.trim())}` : '';
      const response = await fetch(
        `http://127.0.0.1:8000/api/v1/devices/pairing-code${query}`,
        { method: 'POST' },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        setError(body?.detail || 'Falha ao gerar código');
        return;
      }
      const data = await response.json();
      setCode(data.code);
      setExpiresAt(Date.now() + data.expires_in_s * 1000);
    } catch {
      setError('neo-core offline');
    } finally {
      setBusy(false);
    }
  };

  const expired = code !== null && remaining <= 0;

  return (
    <div style={{
      padding: 16, marginBottom: 6,
      background: 'rgba(124, 58, 237, 0.06)',
      border: '1px solid rgba(124, 58, 237, 0.2)',
      borderRadius: 12,
    }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: '#e2e8f0', marginBottom: 4 }}>
        Parear um dispositivo
      </div>
      <div style={{ fontSize: 12, color: '#64748b', marginBottom: 12 }}>
        Gere o código e digite-o no aparelho (aba Dispositivos do neo-desktop).
        Sem pareamento o Neo não controla volume, mídia nem energia daquele aparelho.
      </div>
      <input
        value={userId}
        onChange={(e) => setUserId(e.target.value)}
        placeholder="user_id (UUID da conta)"
        style={{
          width: 340, marginBottom: 12, padding: '6px 10px',
          fontSize: 12, fontFamily: 'monospace',
          background: '#0f1220', color: '#cbd5e1',
          border: '1px solid #334155', borderRadius: 6, outline: 'none',
        }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <button
          onClick={generate}
          disabled={busy}
          style={{
            background: '#1e2235', color: '#e2e8f0',
            border: '1px solid #334155', borderRadius: 8,
            padding: '8px 18px', cursor: busy ? 'default' : 'pointer',
            opacity: busy ? 0.6 : 1,
          }}
        >
          {busy ? 'Gerando…' : code ? 'Gerar outro' : 'Gerar código'}
        </button>
        {code && (
          <>
            <span style={{
              fontSize: 24, fontWeight: 700, letterSpacing: 6,
              fontVariantNumeric: 'tabular-nums',
              color: expired ? '#64748b' : '#a78bfa',
              textDecoration: expired ? 'line-through' : 'none',
            }}>
              {code}
            </span>
            <span style={{ fontSize: 12, color: expired ? '#f87171' : '#64748b' }}>
              {expired ? 'expirado' : `expira em ${remaining}s`}
            </span>
          </>
        )}
      </div>
      {error && <div style={{ marginTop: 10, fontSize: 12, color: '#f87171' }}>❌ {error}</div>}
    </div>
  );
}

/** Remover com confirmação em dois toques — o clique só apaga na segunda vez. */
function RemoveDeviceButton({ device, onRemoved }: { device: DeviceInfo; onRemoved: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const btn = (label: string, danger: boolean, onClick: () => void) => (
    <button
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      disabled={busy}
      style={{
        fontSize: 11, padding: '3px 10px', borderRadius: 6, cursor: 'pointer',
        color: danger ? '#fca5a5' : '#94a3b8',
        background: danger ? 'rgba(248, 113, 113, 0.12)' : 'transparent',
        border: `1px solid ${danger ? 'rgba(248, 113, 113, 0.4)' : '#334155'}`,
        opacity: busy ? 0.5 : 1,
      }}
    >
      {label}
    </button>
  );

  const remove = async () => {
    setBusy(true);
    try {
      const r = await fetch(
        `http://127.0.0.1:8000/api/v1/devices/${encodeURIComponent(device.device_id)}`,
        { method: 'DELETE' },
      );
      if (r.ok) onRemoved();
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  if (!confirming) return btn('Remover', false, () => setConfirming(true));

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }} onClick={(e) => e.stopPropagation()}>
      <span style={{ fontSize: 11, color: '#fca5a5' }}>Remover e desparear?</span>
      {btn('Confirmar', true, remove)}
      {btn('Cancelar', false, () => setConfirming(false))}
    </div>
  );
}

export default function ConnectedDevicesPanel() {
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedDevice, setSelectedDevice] = useState<DeviceInfo | null>(null);

  const fetchDevices = async () => {
    try {
      const response = await fetch('http://127.0.0.1:8000/api/v1/devices');
      if (response.ok) {
        const data = await response.json();
        setDevices(data.devices || []);
        setError(null);
      } else {
        setError('Endpoint em desenvolvimento');
      }
    } catch {
      setError('neo-core offline');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDevices();
    const interval = setInterval(fetchDevices, 10000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flex: 1, gap: 12, color: '#64748b', height: '100%', width: '100%', boxSizing: 'border-box' }}>
        <div style={{ width: 20, height: 20, border: '2px solid #334155', borderTopColor: '#7c3aed', borderRadius: '50%', animation: 'spin 0.8s linear infinite' }} />
        <span>Carregando dispositivos...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flex: 1, gap: 16, color: '#f87171', height: '100%', width: '100%', boxSizing: 'border-box' }}>
        <span>❌ {error}</span>
        <button onClick={fetchDevices} style={{ background: '#1e2235', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 8, padding: '8px 18px', cursor: 'pointer' }}>
          Tentar novamente
        </button>
      </div>
    );
  }

  if (devices.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flex: 1, gap: 10, color: '#475569', textAlign: 'center', height: '100%', width: '100%', boxSizing: 'border-box', padding: 20 }}>
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ opacity: 0.3 }}>
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
          <line x1="8" y1="21" x2="16" y2="21"/>
          <line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
        <p style={{ fontSize: 15, fontWeight: 600, color: '#64748b', margin: 0 }}>Nenhum dispositivo conectado</p>
        <span style={{ fontSize: 12, opacity: 0.7 }}>Dispositivos que usam o neo-core aparecerão aqui</span>
      </div>
    );
  }

  return (
    <>
      {selectedDevice && <DeviceModal device={selectedDevice} onClose={() => setSelectedDevice(null)} />}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 24, overflow: 'auto', flex: 1 }}>
        <PairingCodeCard devices={devices} />
        <div style={{ fontSize: 13, color: '#475569', marginBottom: 8 }}>
          {devices.length} dispositivo{devices.length !== 1 ? 's' : ''} conectado{devices.length !== 1 ? 's' : ''}
        </div>
        {devices.map((device) => (
          <div
            key={device.device_id}
            onClick={() => setSelectedDevice(device)}
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: 14,
              padding: 14,
              background: device.is_current_device ? 'rgba(124, 58, 237, 0.08)' : 'rgba(124, 58, 237, 0.04)',
              border: `1px solid ${device.is_current_device ? 'rgba(124, 58, 237, 0.35)' : 'rgba(124, 58, 237, 0.1)'}`,
              borderRadius: 12,
              cursor: 'pointer',
              transition: 'background 0.15s',
            }}
            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(124, 58, 237, 0.1)')}
            onMouseLeave={e => (e.currentTarget.style.background = device.is_current_device ? 'rgba(124, 58, 237, 0.08)' : 'rgba(124, 58, 237, 0.04)')}
          >
            <div style={{
              width: 40, height: 40, borderRadius: 10,
              background: 'rgba(124, 58, 237, 0.15)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: '#a78bfa', flexShrink: 0,
            }}>
              <DeviceIcon type={device.device_type} />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0, flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: '#e2e8f0' }}>
                  {device.name || (device.device_type === 'neo-mobile' ? 'Neo Mobile' : 'Neo Desktop')}
                </span>
                {device.controllable && (
                  <span style={{ fontSize: 10, padding: '2px 6px', background: 'rgba(59, 130, 246, 0.15)', border: '1px solid rgba(59, 130, 246, 0.35)', borderRadius: 8, color: '#93c5fd' }}>
                    controlável
                  </span>
                )}
                {device.is_current_device && (
                  <span style={{ fontSize: 10, padding: '2px 6px', background: 'rgba(34, 197, 94, 0.15)', border: '1px solid rgba(34, 197, 94, 0.3)', borderRadius: 8, color: '#4ade80' }}>
                    este
                  </span>
                )}
                <span style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  fontSize: 10,
                  padding: '2px 6px',
                  borderRadius: 8,
                  background: device.is_online ? 'rgba(34, 197, 94, 0.15)' : 'rgba(100, 116, 139, 0.15)',
                  border: `1px solid ${device.is_online ? 'rgba(34, 197, 94, 0.3)' : 'rgba(100, 116, 139, 0.3)'}`,
                  color: device.is_online ? '#4ade80' : '#64748b',
                }}>
                  <span style={{
                    width: 6, height: 6, borderRadius: '50%',
                    background: device.is_online ? '#4ade80' : '#64748b',
                  }} />
                  {device.is_online ? 'Online' : 'Offline'}
                </span>
              </div>
              <div style={{ fontSize: 12, color: '#64748b' }}>{device.model}</div>
              <div style={{ display: 'flex', gap: 10, fontSize: 11, color: '#475569', marginTop: 2 }}>
                <span>{device.os_platform} {device.os_version}</span>
                {device.client_version && <span>v{device.client_version}</span>}
              </div>
              <div style={{ fontSize: 10, color: '#334155', marginTop: 4 }}>
                {device.is_online ? 'Online via WebSocket' : 'Offline'}
              </div>
              <div style={{ marginTop: 8 }}>
                <RemoveDeviceButton device={device} onRemoved={fetchDevices} />
              </div>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
