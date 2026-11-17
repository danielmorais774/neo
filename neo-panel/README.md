# neo-panel

Painel web do Neo, para acompanhar e configurar o que roda no servidor: conversas, diários de cada turno, memória, jobs, dispositivos e chaves.

## O que tem

| Área | O que mostra ou faz |
|---|---|
| Chat e sessões | Conversa com o Neo pelo navegador e histórico das sessões |
| Diários | O que aconteceu em cada etapa de um turno: roteamento, chamadas de LLM, ferramentas, tempos |
| Memória | Grafo de conhecimento (PKS) e uma prévia do contexto que vai para o modelo |
| Jobs e agenda | Tarefas em segundo plano, pesquisas e tarefas agendadas |
| Liberações | Pedidos de dado pessoal aguardando aprovação |
| Dispositivos | Aparelhos pareados e conexões ativas por canal |
| Ajustes | Catálogo de modelos por tarefa, chaves dos provedores, skills, Claude Code, transcrição |
| Integrações | WhatsApp (pareamento por QR), Spotify, afeto e o modo de humor do Neo |

## Rodar

```bash
npm install
npm run dev        # Vite; fala com o neo-core em :8000
npm run build
```

O login usa a conta de usuário do neo-core.
