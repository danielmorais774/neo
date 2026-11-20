// Barra de arrastar para ajustar um tamanho do chat largo: a largura de uma coluna
// (`col`, barra em pé) ou a divisória entre seções da coluna (`row`, deitada).
//
// Durante o arraste o valor vai por `onChange` (quem é dono do tamanho re-renderiza
// só a si mesmo — as colunas recebem o conteúdo como `children`, que não
// re-renderiza); no fim vai por `onCommit`, que salva. Teclado: setas, Home/End;
// duplo clique volta ao padrão.

import { useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';

interface ResizeHandleProps {
  /** `col`: entre colunas, arrasta no X. `row`: entre seções, arrasta no Y. */
  axis: 'col' | 'row';
  value: number;
  min: number;
  max: number;
  /** 1: arrastar para a direita/baixo aumenta o valor; -1: diminui. */
  direction: 1 | -1;
  onChange: (value: number) => void;
  onCommit: (value: number) => void;
  onReset: () => void;
  label: string;
  className?: string;
}

const KEY_STEP = 16;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

interface DragState {
  start: number;
  from: number;
  last: number;
  moved: boolean;
}

export function ResizeHandle({
  axis,
  value,
  min,
  max,
  direction,
  onChange,
  onCommit,
  onReset,
  label,
  className,
}: ResizeHandleProps) {
  const drag = useRef<DragState | null>(null);
  const [dragging, setDragging] = useState(false);
  const pointerPos = (event: PointerEvent<HTMLDivElement>) => (axis === 'col' ? event.clientX : event.clientY);
  // Cursor e seleção valem para a página inteira enquanto arrasta: o ponteiro sai
  // da barra (fina) o tempo todo, e sem isto o texto do chat ia sendo selecionado.
  const resizingClass = `neo-resizing-${axis}`;

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { start: pointerPos(event), from: value, last: value, moved: false };
    setDragging(true);
    document.documentElement.classList.add(resizingClass);
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    if (!state) return;
    const next = Math.round(clamp(state.from + direction * (pointerPos(event) - state.start), min, max));
    if (next === state.last) return;
    state.last = next;
    state.moved = true;
    onChange(next);
  };

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    if (!state) return;
    drag.current = null;
    setDragging(false);
    document.documentElement.classList.remove(resizingClass);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (state.moved) onCommit(state.last);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const less = axis === 'col' ? 'ArrowLeft' : 'ArrowUp';
    const more = axis === 'col' ? 'ArrowRight' : 'ArrowDown';
    let next: number | null = null;
    if (event.key === less) next = value - direction * KEY_STEP;
    else if (event.key === more) next = value + direction * KEY_STEP;
    else if (event.key === 'Home') next = min;
    else if (event.key === 'End') next = max;
    if (next === null) return;
    event.preventDefault();
    next = clamp(next, min, max);
    onChange(next);
    onCommit(next);
  };

  return (
    <div
      role="separator"
      aria-orientation={axis === 'col' ? 'vertical' : 'horizontal'}
      aria-label={label}
      aria-valuenow={Math.round(value)}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      title={`${label}: arraste para ajustar, duplo clique volta ao padrão`}
      className={`resize-bar resize-bar-${axis}${dragging ? ' dragging' : ''}${className ? ` ${className}` : ''}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={onReset}
    >
      {axis === 'row' && <span className="resize-bar-grip" aria-hidden="true" />}
    </div>
  );
}

export default ResizeHandle;
