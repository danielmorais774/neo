import Screen, { type TabDef } from '../ui/Screen';
import NeoLogsPanel from '../NeoLogsPanel';
import SessionLogsPanel from '../SessionLogsPanel';

// "Logs" e "Session Logs" eram itens distintos com o mesmo ícone 📋 — ninguém
// adivinhava qual era o tail do app.log e qual era o .md por sessão.
const TABS: TabDef[] = [
  { id: 'app', label: 'App (neo-core)', render: () => <NeoLogsPanel /> },
  { id: 'sessoes', label: 'Por sessão', render: () => <SessionLogsPanel /> },
];

const LogsScreen = () => <Screen icon="logs" title="Logs" tabs={TABS} />;

export default LogsScreen;
