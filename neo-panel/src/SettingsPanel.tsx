import { useState } from 'react';
import Screen, { type TabDef } from './ui/Screen';
import Icon from './ui/Icon';
import ModelosTab from './settings/ModelosTab';
import ProvidersTab from './settings/ProvidersTab';
import VozTab from './settings/VozTab';
import ArquivosTab from './settings/ArquivosTab';
import ClaudeCodeTab from './settings/ClaudeCodeTab';
import SessionsTab from './SessionsTab';
import ConnectedDevicesPanel from './ConnectedDevicesPanel';

const SettingsPanel = () => {
  const [reloadToken, setReloadToken] = useState(0);

  const tabs: TabDef[] = [
    { id: 'modelos', label: 'Modelos', render: () => <ModelosTab reloadToken={reloadToken} /> },
    { id: 'providers', label: 'Providers', render: () => <ProvidersTab reloadToken={reloadToken} /> },
    { id: 'voz', label: 'Voz', render: () => <VozTab reloadToken={reloadToken} /> },
    { id: 'arquivos', label: 'Arquivos', render: () => <ArquivosTab reloadToken={reloadToken} /> },
    { id: 'claude-code', label: 'Claude Code', render: () => <ClaudeCodeTab reloadToken={reloadToken} /> },
    { id: 'comunicacoes', label: 'Comunicações', render: () => <SessionsTab /> },
    { id: 'dispositivos', label: 'Dispositivos', render: () => <ConnectedDevicesPanel /> },
  ];

  return (
    <Screen
      icon="gear"
      title="Configurações"
      tabs={tabs}
      actions={
        <button className="neo-btn-ghost" onClick={() => setReloadToken((n) => n + 1)}>
          <Icon name="refresh" size={14} />
          Recarregar
        </button>
      }
    />
  );
};

export default SettingsPanel;
