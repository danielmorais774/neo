import type { ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import Icon, { type IconName } from './Icon';

export interface TabDef {
  id: string;
  label: string;
  render: () => ReactNode;
}

interface ScreenProps {
  icon: IconName;
  title: string;
  actions?: ReactNode;
  tabs?: TabDef[];
  children?: ReactNode;
}

/**
 * Casca das telas: topbar + abas opcionais + corpo.
 *
 * A aba ativa vive em ?tab= — é o que deixa os redirects das rotas antigas
 * (/whatsapp → /comunicacao?tab=conversas) caírem no lugar certo, e o link de
 * uma aba continuar colável.
 */
const Screen = ({ icon, title, actions, tabs, children }: ScreenProps) => {
  const [params, setParams] = useSearchParams();

  const requested = params.get('tab');
  const active = tabs?.find((t) => t.id === requested) ?? tabs?.[0];

  return (
    <div className="neo-screen">
      <div className="neo-topbar">
        <div className="neo-topbar-title">
          <Icon name={icon} />
          <span>{title}</span>
        </div>
        {actions}
      </div>

      {tabs && tabs.length > 1 && (
        <div className="neo-tabbar">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={`neo-tab${tab.id === active?.id ? ' is-active' : ''}`}
              onClick={() => setParams({ tab: tab.id }, { replace: true })}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      <div className="neo-screen-body">
        {active ? active.render() : children}
      </div>
    </div>
  );
};

export default Screen;
