import Screen, { type TabDef } from '../ui/Screen';
import NeoVisionPanel from '../NeoVisionPanel';
import NeoFrameAnalyzer from '../NeoFrameAnalyzer';

// Os dois falam com o mesmo serviço (neo-vision) — eram dois itens de menu
// para o que é vídeo vs. quadro único.
const TABS: TabDef[] = [
  { id: 'video', label: 'Vídeo', render: () => <NeoVisionPanel /> },
  { id: 'frame', label: 'Frame', render: () => <NeoFrameAnalyzer /> },
];

const VisaoScreen = () => <Screen icon="vision" title="Visão" tabs={TABS} />;

export default VisaoScreen;
