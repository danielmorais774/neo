import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import WorkingMemoryViewer from './components/WorkingMemoryViewer'
import JobsViewer from './components/JobsViewer'
import { WindowService } from './lib'
import { registerViewerShortcuts } from './lib/viewerShortcuts'
import { ThemeProvider } from './themes/ThemeProvider'
import './index.css'
import './electronAPI-init'

const mode = new URLSearchParams(window.location.search).get('mode')

/** Janela própria do caderno de trabalho (`open_working_memory_window` no Rust).
 *
 * O desvio é AQUI, e não dentro do App como o do `quick-help`: quando o App
 * monta, ele já ligou dois efeitos que não fazem sentido nesta janela — o
 * polling de status do core a cada 8s e o `setNotificationContext('chat')`, que
 * mentiria para o roteador de notificações dizendo que a aba ativa é o chat.
 * A regra de hooks impede sair antes deles lá dentro; aqui o App nem nasce.
 */
const isNotebookWindow = mode === 'working-memory'
/** Janela própria das tarefas em background (`open_jobs_window`), mesmo desvio. */
const isJobsWindow = mode === 'jobs'

// Fundo SÓLIDO, ao contrário das outras janelas: esta tem decoração nativa e
// `transparent: false`, e o `--modal-bg` do tema é translúcido — sobre janela
// opaca ele mostraria o cinza do sistema em vez do vidro. `--sessions-bg` é o
// token opaco que os quatro temas já definem.
//
// Fora de qualquer componente de propósito: é preparo da PÁGINA, feito uma vez
// antes do React montar. Dentro do render seria efeito colateral em cada
// renderização (e a regra `react-hooks/immutability` reprova, com razão).
if (isNotebookWindow || isJobsWindow) {
  const solid = 'var(--sessions-bg, #0b101c)'
  document.documentElement.style.background = solid
  document.body.style.background = solid
}

// ⌘⇧M abre o caderno e ⌘⇧J as tarefas. Registrado AQUI e não em cada janela:
// as três (chat, caderno, tarefas) carregam este mesmo `index.html`, então um
// registro só vale para todas e dá para pular de uma para a outra pelo teclado.
registerViewerShortcuts()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      {isNotebookWindow ? (
        <WorkingMemoryViewer standalone onClose={() => WindowService.close()} />
      ) : isJobsWindow ? (
        <JobsViewer onClose={() => WindowService.close()} />
      ) : (
        <App />
      )}
    </ThemeProvider>
  </React.StrictMode>,
)
