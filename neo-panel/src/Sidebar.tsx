import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import Icon, { type IconName } from './ui/Icon';

interface NavItem {
  path: string;
  label: string;
  icon: IconName;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

/*
 * A lista era plana e tinha 18 itens. O corte aqui é por natureza da tela:
 * o que se usa no dia a dia (operação), o que é a cabeça do neo (mente), o que
 * se olha quando algo quebra (diagnóstico) e o que se configura uma vez
 * (sistema). WhatsApp deixou de existir como três entradas separadas — o QR
 * virou Config › Conexões (é setup), e conversas + missões viraram Comunicação.
 */
const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Operação',
    items: [
      { path: '/chat', label: 'Chat', icon: 'robot' },
      { path: '/comunicacao', label: 'Comunicação', icon: 'chat' },
      { path: '/musica', label: 'Música', icon: 'music' },
      { path: '/pesquisas', label: 'Pesquisas', icon: 'search' },
      { path: '/servicos', label: 'Serviços', icon: 'gear' },
      { path: '/arquivos', label: 'Arquivos', icon: 'file' },
      { path: '/dados', label: 'Dados', icon: 'table' },
      { path: '/sirene', label: 'Sirene', icon: 'siren' },
    ],
  },
  {
    label: 'Mente',
    items: [
      { path: '/memoria', label: 'Memória', icon: 'brain' },
      { path: '/emocoes', label: 'Emoções', icon: 'emotion' },
      { path: '/skills', label: 'Skills', icon: 'skill' },
    ],
  },
  {
    label: 'Olhos',
    items: [
      { path: '/visao', label: 'Visão', icon: 'vision' },
    ],
  },
  {
    label: 'Diagnóstico',
    items: [
      { path: '/sessoes', label: 'Sessões', icon: 'bug' },
      { path: '/logs', label: 'Logs', icon: 'logs' },
      { path: '/wake', label: 'Wake · Treino', icon: 'mic' },
    ],
  },
  {
    label: 'Sistema',
    items: [
      { path: '/config', label: 'Config', icon: 'gear' },
    ],
  },
];

const Sidebar = () => {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();

  return (
    <div className="neo-sidebar" style={{ width: collapsed ? 72 : 280 }}>
      <div className="neo-brand" style={collapsed ? { justifyContent: 'center', padding: '6px 0 22px' } : undefined}>
        <div className="neo-brand-mark">
          <div className="neo-brand-pulse" />
        </div>
        {!collapsed && (
          <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.15 }}>
            <span className="neo-brand-name">Neo</span>
            <span className="neo-brand-sub">Panel</span>
          </div>
        )}
      </div>

      <nav className="neo-nav">
        {NAV_GROUPS.map((group) => (
          <div key={group.label}>
            {collapsed ? (
              <div className="neo-nav-group-rule" />
            ) : (
              <div className="neo-nav-group-label">{group.label}</div>
            )}
            {group.items.map((item) => {
              const isActive = location.pathname === item.path;
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  className={
                    'neo-nav-item' +
                    (isActive ? ' is-active' : '') +
                    (collapsed ? ' is-collapsed' : '')
                  }
                  title={collapsed ? item.label : undefined}
                >
                  {isActive && <div className="neo-nav-bar" />}
                  <Icon name={item.icon} />
                  {!collapsed && <span>{item.label}</span>}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <button
        className="neo-collapse"
        onClick={() => setCollapsed(!collapsed)}
        style={collapsed ? { justifyContent: 'center' } : undefined}
        title={collapsed ? 'Expandir' : 'Recolher'}
      >
        <span style={{ fontSize: 15 }}>{collapsed ? '→' : '←'}</span>
        {!collapsed && <span>Recolher</span>}
      </button>
    </div>
  );
};

export default Sidebar;
