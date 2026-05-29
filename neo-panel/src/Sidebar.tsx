import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';

interface NavItem {
  path: string;
  label: string;
  icon: string;
}

const NAV_ITEMS: NavItem[] = [
  { path: '/whatsapp-status', label: 'WhatsApp Status', icon: '🔑' },
  { path: '/whatsapp', label: 'WhatsApp', icon: '📱' },
  { path: '/debug', label: 'Debug', icon: '🔬' },
  { path: '/session-logs', label: 'Session Logs', icon: '📝' },
  { path: '/vision', label: 'Vision', icon: '🎬' },
  { path: '/frame', label: 'Frame', icon: '🔍' },
  { path: '/knowledge-graph', label: 'Knowledge Graph', icon: '🕸️' },
  { path: '/missions', label: 'Missões', icon: '📋' },
  { path: '/devices', label: 'Dispositivos', icon: '💻' },
  { path: '/logs', label: 'Logs', icon: '📋' },
  { path: '/letta', label: 'Letta', icon: '🤖' },
  { path: '/pks', label: 'PKS', icon: '🧠' },
];

const Sidebar = () => {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();

  return (
    <div style={{
      width: collapsed ? 60 : 220,
      background: '#13161f',
      borderRight: '1px solid #1e2235',
      display: 'flex',
      flexDirection: 'column',
      transition: 'width 0.2s ease',
      flexShrink: 0,
    }}>
      {/* Header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: collapsed ? 'center' : 'flex-start',
        padding: collapsed ? '16px 8px' : '16px 20px',
        borderBottom: '1px solid #1e2235',
        gap: 12,
      }}>
        <img
          src="https://api.dicebear.com/7.x/bottts-neutral/svg?seed=neo&backgroundColor=7c3aed"
          alt="Neo Avatar"
          width={collapsed ? 28 : 24}
          height={collapsed ? 28 : 24}
          style={{ borderRadius: 6, objectFit: 'cover', flexShrink: 0 }}
        />
        {!collapsed && (
          <>
            <span style={{ color: '#7c3aed', fontWeight: 700, fontSize: 16 }}>Neo</span>
            <span style={{ color: '#64748b', fontWeight: 400, fontSize: 14 }}>Panel</span>
          </>
        )}
      </div>

      {/* Navigation */}
      <nav style={{ flex: 1, padding: '8px 0', overflowY: 'auto' }}>
        {NAV_ITEMS.map((item) => {
          const isActive = location.pathname === item.path;
          return (
            <Link
              key={item.path}
              to={item.path}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: collapsed ? '12px 8px' : '12px 20px',
                color: isActive ? '#a78bfa' : '#64748b',
                background: isActive ? 'rgba(124, 58, 237, 0.1)' : 'transparent',
                textDecoration: 'none',
                fontSize: 14,
                fontWeight: isActive ? 600 : 400,
                transition: 'all 0.15s',
                borderLeft: isActive ? '3px solid #7c3aed' : '3px solid transparent',
                justifyContent: collapsed ? 'center' : 'flex-start',
              }}
              title={collapsed ? item.label : undefined}
            >
              <span style={{ fontSize: 18 }}>{item.icon}</span>
              {!collapsed && <span>{item.label}</span>}
            </Link>
          );
        })}
      </nav>

      {/* Collapse button */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 16,
          border: 'none',
          background: 'transparent',
          color: '#64748b',
          cursor: 'pointer',
          borderTop: '1px solid #1e2235',
          fontSize: 18,
        }}
        title={collapsed ? 'Expand' : 'Collapse'}
      >
        {collapsed ? '→' : '←'}
      </button>
    </div>
  );
};

export default Sidebar;