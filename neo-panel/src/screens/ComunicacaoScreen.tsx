import Screen, { type TabDef } from '../ui/Screen';
import WhatsAppPanel from '../WhatsAppPanel';
import MissionsPanel from '../MissionsPanel';

/*
 * "Missões" era um item de menu solto, mas o modelo dela tem contact_jid e
 * channel: é missão DE WhatsApp. Fica ao lado das conversas.
 *
 * O QR/auth não está aqui de propósito — é setup, e mora em Config › Conexões.
 */
const TABS: TabDef[] = [
  { id: 'conversas', label: 'Conversas', render: () => <WhatsAppPanel /> },
  { id: 'missoes', label: 'Missões', render: () => <MissionsPanel /> },
];

const ComunicacaoScreen = () => <Screen icon="chat" title="Comunicação" tabs={TABS} />;

export default ComunicacaoScreen;
