// Molduras das colunas do chat largo com os tamanhos arrastáveis (App.tsx).
//
// Cada moldura é dona do próprio tamanho: arrastar re-renderiza só ela. O conteúdo
// chega como `children`/props já criados pelo App, e o React não re-renderiza um
// elemento que não mudou — nem a coluna, nem o ChatPage ao lado, que só sofre o
// reflow da largura (como quando a janela muda de tamanho).

import { useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { readPaneSize, storePaneSize } from '../lib/chatPrefs';
import { ResizeHandle } from './ResizeHandle';

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

// Limites da largura: abaixo disso a lista de conversas e os cartões quebram
// demais; acima, a conversa no centro fica espremida a 1280 px.
const WIDTH_LIMITS = {
  left: [240, 440],
  right: [280, 480],
} as const;

interface SideColumnProps {
  side: 'left' | 'right';
  /** Largura de quando a pessoa nunca arrastou (depende da largura da janela). */
  defaultWidth: number;
  ariaLabel: string;
  className?: string;
  children: ReactNode;
}

export function SideColumn({ side, defaultWidth, ariaLabel, className, children }: SideColumnProps) {
  const [stored, setStored] = useState<number | null>(() => readPaneSize(side));
  const [live, setLive] = useState<number | null>(null);
  const [min, max] = WIDTH_LIMITS[side];
  const width = clamp(live ?? stored ?? defaultWidth, min, max);

  return (
    <aside
      className={`side-col side-${side}${className ? ` ${className}` : ''}`}
      style={{ width }}
      aria-label={ariaLabel}
    >
      {children}
      <ResizeHandle
        axis="col"
        value={width}
        min={min}
        max={max}
        direction={side === 'left' ? 1 : -1}
        onChange={setLive}
        onCommit={(next) => {
          setStored(next);
          setLive(null);
          storePaneSize(side, next);
        }}
        onReset={() => {
          setStored(null);
          setLive(null);
          storePaneSize(side, null);
        }}
        label={side === 'left' ? 'Largura das conversas' : 'Largura do segundo plano'}
        className={`resize-bar-${side}`}
      />
    </aside>
  );
}

// Divisória da coluna esquerda: Conversas em cima, "Agentes e canais" embaixo. O
// tamanho salvo é a ALTURA DE BAIXO, para a seção não mudar quando a janela cresce.
const MIN_TOP = 180;
const MIN_BOTTOM = 110;
const DEFAULT_BOTTOM_RATIO = 0.36;
const FALLBACK_BOTTOM = 240;

interface ColumnSplitProps {
  top: ReactNode;
  bottom: ReactNode;
}

export function ColumnSplit({ top, bottom }: ColumnSplitProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);
  const [stored, setStored] = useState<number | null>(() => readPaneSize('sessionPanel'));
  const [live, setLive] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setHeight(Math.round(entry.contentRect.height)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const max = Math.max(MIN_BOTTOM, height - MIN_TOP);
  const fallback = height ? Math.round(height * DEFAULT_BOTTOM_RATIO) : FALLBACK_BOTTOM;
  const bottomHeight = clamp(live ?? stored ?? fallback, MIN_BOTTOM, max);

  return (
    <div className="col-split" ref={ref}>
      <div className="col-split-top">{top}</div>
      <ResizeHandle
        axis="row"
        value={bottomHeight}
        min={MIN_BOTTOM}
        max={max}
        direction={-1}
        onChange={setLive}
        onCommit={(next) => {
          setStored(next);
          setLive(null);
          storePaneSize('sessionPanel', next);
        }}
        onReset={() => {
          setStored(null);
          setLive(null);
          storePaneSize('sessionPanel', null);
        }}
        label="Altura de agentes e canais"
      />
      <div className="col-split-bottom" style={{ height: bottomHeight }}>
        {bottom}
      </div>
    </div>
  );
}
