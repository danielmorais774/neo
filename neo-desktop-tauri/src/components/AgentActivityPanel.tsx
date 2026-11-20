// Coluna esquerda do chat largo, embaixo: agentes e canais da conversa aberta.
// Fica sempre na tela: quem está em ação primeiro, depois quem já trabalhou nesta
// conversa, e um aviso quando ainda não houve ninguém.

import { useAgentActivity, type AgentActivity, type AgentActivityState } from '../hooks/useAgentActivity';
import { humanizeTime } from '../hooks/useSessionManagement';
import type { AgentId } from '../lib/agentActivity';
import { ColumnIcon, type ColumnIconName } from './ColumnIcon';
import './AgentActivityPanel.css';

const AGENT_ICON: Record<AgentId, ColumnIconName> = {
  email: 'mail',
  browser: 'globe',
  browser_server: 'server',
  web: 'search',
  sandbox: 'terminal',
  files: 'file',
  notebook: 'book',
  claude: 'code',
  whatsapp: 'message',
  home_assistant: 'home',
  computer: 'monitor',
};

const STATE_LABEL: Record<AgentActivityState, string> = {
  busy: 'em ação',
  queued: 'na fila',
  waiting: 'esperando você',
  idle: 'parado',
};

function AgentRow({ activity }: { activity: AgentActivity }) {
  const idle = activity.state === 'idle';
  const when = idle && activity.at ? humanizeTime(new Date(activity.at).toISOString()) : '';
  return (
    <li className={`agent-row agent-row-${activity.state}`}>
      <span className="agent-icon">
        <ColumnIcon name={AGENT_ICON[activity.agent]} />
      </span>
      <span className="agent-text">
        <span className="agent-top">
          <span className="agent-name">{activity.name}</span>
          <span className={`agent-pill agent-pill-${activity.state}`}>
            <span className="agent-pill-dot" />
            {activity.label ?? STATE_LABEL[activity.state]}
          </span>
        </span>
        {/* key no passo: a frase nova entra com o fade, a velha não pisca. */}
        <span key={activity.step} className="agent-step" title={activity.step}>
          {idle && when ? `${when} · ${activity.step}` : activity.step}
        </span>
      </span>
      {activity.state === 'busy' && (
        <span className="agent-bar" aria-hidden="true">
          <span />
        </span>
      )}
    </li>
  );
}

export function AgentActivityPanel() {
  const agents = useAgentActivity();
  const working = agents.filter((activity) => activity.state !== 'idle').length;

  return (
    <section className="agents-panel" aria-labelledby="agents-panel-title">
      <div className="agents-head">
        <h3 id="agents-panel-title">Agentes e canais</h3>
        {working > 0 ? (
          <span className="agents-count">
            <span className="agents-pulse" />
            {working === 1 ? '1 trabalhando' : `${working} trabalhando`}
          </span>
        ) : (
          <span className="agents-count agents-count-idle">ninguém agora</span>
        )}
      </div>
      {agents.length ? (
        <ul className="agents-list">
          {agents.map((activity) => (
            <AgentRow key={activity.key} activity={activity} />
          ))}
        </ul>
      ) : (
        <p className="agents-empty">
          Ninguém trabalhando nesta conversa ainda. Quando o Neo usar o email, o navegador, o sandbox ou
          outro canal, ele aparece aqui.
        </p>
      )}
    </section>
  );
}

export default AgentActivityPanel;
