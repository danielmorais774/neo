/*
 * Ícones do design "Redesign do dashboard de agente" (Claude Design).
 * Substituem os emoji da sidebar antiga: emoji não aceitam cor, então o estado
 * ativo/inativo não conseguia se expressar no ícone.
 */

import type { ReactNode } from 'react';

export type IconName =
  | 'key' | 'chat' | 'bug' | 'logs' | 'vision' | 'frame' | 'graph'
  | 'mission' | 'search' | 'device' | 'robot' | 'brain' | 'emotion'
  | 'skill' | 'music' | 'gear' | 'refresh' | 'warn' | 'file' | 'mic'
  | 'siren' | 'table' | 'clock';

const PATHS: Record<IconName, ReactNode> = {
  // Cadastros generativos (/dados): grade de dados — lista + colunas.
  table: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M3 10h18M9 10v9M15 10v9" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="15" r="3.2" />
      <path d="M10.3 12.7 19 4M19 4h-3.5M19 4v3.5" />
    </>
  ),
  chat: <path d="M4 6h16v10H8l-4 4V6z" strokeLinejoin="round" />,
  bug: (
    <>
      <rect x="8" y="7" width="8" height="10" rx="4" />
      <path d="M12 7V4M8.5 9 5.5 7M15.5 9l3-2M8.5 15l-3 2M15.5 15l3 2M8 12H4M16 12h4" />
    </>
  ),
  logs: (
    <>
      <path d="M6 4h9l3 3v13H6z" strokeLinejoin="round" />
      <path d="M9 11h6M9 14h6M9 17h4" strokeLinejoin="round" />
    </>
  ),
  vision: (
    <>
      <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z" />
      <circle cx="12" cy="12" r="2.6" />
    </>
  ),
  frame: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path strokeLinecap="round" d="M20 20l-4-4" />
    </>
  ),
  graph: (
    <>
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="18" cy="6" r="2.2" />
      <circle cx="12" cy="18" r="2.2" />
      <path d="M7.7 7.5 10.5 16.2M16.3 7.5 13.5 16.2M8 6h8" strokeLinejoin="round" />
    </>
  ),
  mission: (
    <>
      <rect x="5" y="4" width="14" height="17" rx="1.5" strokeLinejoin="round" />
      <path d="M9 3.5h6v2H9z" strokeLinejoin="round" />
      <path strokeLinecap="round" d="M9 12h6M9 16h4" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path strokeLinecap="round" d="M20 20l-4.3-4.3" />
    </>
  ),
  device: (
    <>
      <rect x="3" y="5" width="18" height="12" rx="1.5" strokeLinejoin="round" />
      <path strokeLinecap="round" d="M8 20h8M12 17v3" />
    </>
  ),
  robot: (
    <>
      <rect x="5" y="9" width="14" height="10" rx="2.5" strokeLinejoin="round" />
      <circle cx="9.5" cy="14" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="14.5" cy="14" r="1.1" fill="currentColor" stroke="none" />
      <path strokeLinecap="round" d="M12 9V6M9 6h6" />
    </>
  ),
  brain: (
    <>
      <path
        strokeLinejoin="round"
        d="M9 4.5A2.5 2.5 0 0 0 6.5 7v.3A2.7 2.7 0 0 0 5 9.7v1A2.7 2.7 0 0 0 6 13v1.3A2.7 2.7 0 0 0 8.7 17H9V4.5zM15 4.5A2.5 2.5 0 0 1 17.5 7v.3A2.7 2.7 0 0 1 19 9.7v1A2.7 2.7 0 0 1 18 13v1.3A2.7 2.7 0 0 1 15.3 17H15V4.5z"
      />
      <path strokeLinecap="round" d="M9 17v2.5M15 17v2.5" />
    </>
  ),
  emotion: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path strokeLinecap="round" d="M9 10.2h.01M15 10.2h.01M8.5 14.5c1 1.2 2.2 1.8 3.5 1.8s2.5-.6 3.5-1.8" />
    </>
  ),
  skill: <path d="M4 20l6-6M14 4l6 6-4 1-3 3-3-3 3-3z" strokeLinejoin="round" strokeLinecap="round" />,
  music: (
    <>
      <path d="M9 18V5l11-2v13" strokeLinejoin="round" />
      <circle cx="6.5" cy="18" r="2.5" />
      <circle cx="17.5" cy="16" r="2.5" />
    </>
  ),
  gear: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path
        strokeLinejoin="round"
        d="M19.4 13.5a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H4.5a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H10a1.7 1.7 0 0 0 1-1.5V4.5a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"
      />
    </>
  ),
  refresh: (
    <>
      <path d="M4 4v5h5M20 20v-5h-5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M19 9a7.5 7.5 0 0 0-13-3.5L4 9M5 15a7.5 7.5 0 0 0 13 3.5L20 15" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  warn: (
    <>
      <path d="M12 3 2 20h20L12 3z" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M12 10v4M12 17h.01" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  file: (
    <>
      <path d="M7 3h7l4 4v14H7z" strokeLinejoin="round" />
      <path d="M14 3v4h4" strokeLinejoin="round" />
      <path strokeLinecap="round" d="M9.5 13h5M9.5 16.5h5" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path strokeLinecap="round" d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6" />
    </>
  ),
  // Giroflex: cúpula + base + raios de luz saindo.
  siren: (
    <>
      <path d="M7 16a5 5 0 0 1 10 0z" strokeLinejoin="round" />
      <rect x="5" y="16" width="14" height="3.5" rx="1" strokeLinejoin="round" />
      <path strokeLinecap="round" d="M12 4v2.5M5.5 6.5 7.2 8.2M18.5 6.5 16.8 8.2" />
    </>
  ),
  // Agenda (/agenda): relógio — mostrador + ponteiros.
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path strokeLinecap="round" d="M12 7.5V12l3 2" />
    </>
  ),
};

// refresh/warn são desenhados com traço mais grosso no design original.
const STROKE: Partial<Record<IconName, number>> = { refresh: 1.8, warn: 1.8 };

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
}

const Icon = ({ name, size = 18, className = 'neo-nav-icon' }: IconProps) => (
  <span className={className} style={{ width: size, height: size }}>
    <svg
      viewBox="0 0 24 24"
      width="100%"
      height="100%"
      fill="none"
      stroke="currentColor"
      strokeWidth={STROKE[name] ?? 1.6}
      strokeLinecap="round"
    >
      {PATHS[name]}
    </svg>
  </span>
);

export default Icon;
