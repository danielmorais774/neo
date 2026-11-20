// Coluna esquerda do chat largo, embaixo: quem está trabalhando AGORA na
// conversa aberta. Só lista quem está em ação; sem ninguém, a seção some.

import { useAgentActivity, type AgentActivityState } from '../hooks/useAgentActivity';
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
};

export function AgentActivityPanel() {
  const agents = useAgentActivity();
  if (!agents.length) return null;

  return (
    <section className="agents-panel" aria-labelledby="agents-panel-title">
      <div className="agents-head">
        <h3 id="agents-panel-title">Agentes em ação</h3>
        <span className="agents-count">
          <span className="agents-pulse" />
          {agents.length === 1 ? '1 trabalhando' : `${agents.length} trabalhando`}
        </span>
      </div>
      <ul className="agents-list">
        {agents.map((activity) => (
          <li key={activity.key} className={`agent-row agent-row-${activity.state}`}>
            <span className="agent-icon">
              <ColumnIcon name={AGENT_ICON[activity.agent]} />
            </span>
            <span className="agent-text">
              <span className="agent-top">
                <span className="agent-name">{activity.name}</span>
                <span className={`agent-pill agent-pill-${activity.state}`}>
                  <span className="agent-pill-dot" />
                  {STATE_LABEL[activity.state]}
                </span>
              </span>
              {/* key no passo: a frase nova entra com o fade, a velha não pisca. */}
              <span key={activity.step} className="agent-step" title={activity.step}>
                {activity.step}
              </span>
            </span>
            {activity.state === 'busy' && (
              <span className="agent-bar" aria-hidden="true">
                <span />
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export default AgentActivityPanel;
