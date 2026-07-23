import { useState } from 'react';
import Screen, { type TabDef } from './ui/Screen';
import Icon from './ui/Icon';
import ModelosTab from './settings/ModelosTab';
import VozTab from './settings/VozTab';
import WhatsAppStatusPanel from './WhatsAppStatusPanel';
import ConnectedDevicesPanel from './ConnectedDevicesPanel';

/*
 * Config deixou de ser um scroll único de cinco blocos. As abas existem porque
 * a tela passou a absorver o que era item de sidebar: o QR do WhatsApp
 * (conexão, não conversa) e os dispositivos pareados.
 *
 * Conexões é também onde o Service Gateway (F2) entra quando chegar — daí ela
 * já nascer separada de Dispositivos em vez de "Integrações" genérico.
 */
const SettingsPanel = () => {
  // Recarregar na topbar: cada aba refaz seus fetches quando o token muda.
  const [reloadToken, setReloadToken] = useState(0);

  const tabs: TabDef[] = [
    { id: 'modelos', label: 'Modelos', render: () => <ModelosTab reloadToken={reloadToken} /> },
    { id: 'voz', label: 'Voz', render: () => <VozTab reloadToken={reloadToken} /> },
    { id: 'conexoes', label: 'Conexões', render: () => <WhatsAppStatusPanel /> },
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
