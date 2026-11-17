import Screen, { type TabDef } from '../ui/Screen';
import PKSPanel from '../PKSPanel';
import KnowledgeGraphPanel from '../KnowledgeGraphPanel';
import PKSContextDebugPanel from '../PKSContextDebugPanel';

/*
 * PKS, Grafo e PKS Context eram três itens de menu sobre o mesmo dado: o que o
 * neo sabe. Contexto fica junto (e não em Diagnóstico) porque só se usa ele
 * olhando pro que está nas outras duas abas.
 */
const TABS: TabDef[] = [
  { id: 'pks', label: 'PKS', render: () => <PKSPanel /> },
  { id: 'grafo', label: 'Grafo', render: () => <KnowledgeGraphPanel /> },
  { id: 'contexto', label: 'Contexto (debug)', render: () => <PKSContextDebugPanel /> },
];

const MemoriaScreen = () => <Screen icon="brain" title="Memória" tabs={TABS} />;

export default MemoriaScreen;
