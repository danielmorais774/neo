import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import './GlassSelect.css';

const MENU_GAP = 8; // distância entre o gatilho e o menu
const VIEWPORT_MARGIN = 8; // folga mínima até a borda da janela
const MIN_MENU_HEIGHT = 120;
const MIN_MENU_WIDTH = 210;
const MAX_MENU_WIDTH = 360;

export type GlassSelectOption<T extends string> = {
  value: T;
  label: string;
  description?: string;
};

interface GlassSelectProps<T extends string> {
  value: T;
  options: Array<GlassSelectOption<T>>;
  onChange: (value: T) => void;
  disabled?: boolean;
  ariaLabel: string;
  width?: number;
}

function GlassSelect<T extends string>({
  value,
  options,
  onChange,
  disabled,
  ariaLabel,
  width
}: GlassSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties | null>(null);

  const close = () => {
    setOpen(false);
    setMenuStyle(null);
  };

  const selected = useMemo(() => {
    return options.find(o => o.value === value) ?? options[0];
  }, [options, value]);

  useEffect(() => {
    if (!open) return;

    const onMouseDown = (e: MouseEvent) => {
      const rootEl = rootRef.current;
      const menuEl = menuRef.current;
      if (!rootEl) return;

      const target = e.target as Node | null;
      if (target && (rootEl.contains(target) || (menuEl && menuEl.contains(target)))) {
        return;
      }
      close();
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };

    window.addEventListener('mousedown', onMouseDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('mousedown', onMouseDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  // Posiciona o menu (portal) MEDINDO o DOM — antes estimava 44px/opção com
  // teto de 400px e não tinha max-height: com a lista de modelos crescendo, o
  // menu real ficava maior que a estimativa e vazava pela borda da janela
  // (opções ocultas, sem scroll). Agora: abre para o lado com mais espaço,
  // ancora pela borda do gatilho (bottom quando abre para cima), limita a
  // altura ao espaço disponível e rola por dentro.
  useLayoutEffect(() => {
    if (!open) return;

    const updatePosition = () => {
      const rootEl = rootRef.current;
      const menuEl = menuRef.current;
      if (!rootEl || !menuEl) return;
      const rect = rootEl.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;

      // scrollHeight é a altura do conteúdo inteiro, mesmo com max-height.
      const naturalH = menuEl.scrollHeight;
      const spaceBelow = vh - rect.bottom - MENU_GAP - VIEWPORT_MARGIN;
      const spaceAbove = rect.top - MENU_GAP - VIEWPORT_MARGIN;
      const showBelow = naturalH <= spaceBelow || spaceBelow >= spaceAbove;
      const maxHeight = Math.max(MIN_MENU_HEIGHT, showBelow ? spaceBelow : spaceAbove);

      const maxWidth = Math.max(160, Math.min(MAX_MENU_WIDTH, vw - VIEWPORT_MARGIN * 2));
      const minWidth = Math.min(maxWidth, Math.max(MIN_MENU_WIDTH, rect.width));
      // Aplica os limites ANTES de medir a largura: com max-height a barra de
      // rolagem aparece e alarga o menu — medido sem ela, o menu encostado na
      // borda direita vazava a largura da barra para fora da janela.
      menuEl.style.maxHeight = `${maxHeight}px`;
      menuEl.style.maxWidth = `${maxWidth}px`;
      menuEl.style.minWidth = `${minWidth}px`;
      const width = menuEl.offsetWidth;
      const left = Math.max(
        VIEWPORT_MARGIN,
        Math.min(rect.left, vw - VIEWPORT_MARGIN - width)
      );

      setMenuStyle({
        position: 'fixed',
        left: `${left}px`,
        ...(showBelow
          ? { top: `${rect.bottom + MENU_GAP}px` }
          : { bottom: `${vh - rect.top + MENU_GAP}px` }),
        minWidth: `${minWidth}px`,
        maxWidth: `${maxWidth}px`,
        maxHeight: `${maxHeight}px`,
        zIndex: 9999
      });
    };

    const onScroll = (e: Event) => {
      // Rolar a própria lista não move o gatilho: não recalcula.
      const menuEl = menuRef.current;
      if (menuEl && e.target instanceof Node && menuEl.contains(e.target)) return;
      updatePosition();
    };

    updatePosition();
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [open, options]);

  // Ao abrir, centraliza a opção selecionada na área visível da lista
  // (uma vez por abertura, depois que a altura máxima já foi aplicada).
  const scrolledForOpenRef = useRef(false);
  useLayoutEffect(() => {
    if (!open) {
      scrolledForOpenRef.current = false;
      return;
    }
    const menuEl = menuRef.current;
    if (!menuStyle || !menuEl || scrolledForOpenRef.current) return;
    scrolledForOpenRef.current = true;
    const sel = menuEl.querySelector<HTMLElement>('.glass-select-option.selected');
    if (!sel || menuEl.scrollHeight <= menuEl.clientHeight) return;
    menuEl.scrollTop = sel.offsetTop - (menuEl.clientHeight - sel.offsetHeight) / 2;
  }, [open, menuStyle]);

  const handleSelect = (next: T) => {
    onChange(next);
    close();
  };

  const menu = (
    <div
      ref={menuRef}
      className="glass-select-menu portal"
      role="listbox"
      aria-label={ariaLabel}
      style={menuStyle ?? { position: 'fixed', left: 0, top: 0, visibility: 'hidden' }}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="option"
          aria-selected={opt.value === value}
          className={`glass-select-option ${opt.value === value ? 'selected' : ''}`}
          title={opt.description ? `${opt.label} — ${opt.description}` : opt.label}
          onClick={() => handleSelect(opt.value)}
        >
          <span className="glass-select-option-main">{opt.label}</span>
          {opt.description && (
            <span className="glass-select-option-sub">{opt.description}</span>
          )}
          {opt.value === value && (
            <span className="glass-select-check" aria-hidden="true">✓</span>
          )}
        </button>
      ))}
    </div>
  );

  return (
    <div
      ref={rootRef}
      className={`glass-select ${disabled ? 'disabled' : ''} ${open ? 'open' : ''}`}
      style={width ? { width } : undefined}
    >
      <button
        type="button"
        className="glass-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <span className="glass-select-text">
          <span className="glass-select-label">{selected?.label}</span>
          {selected?.description && (
            <span className="glass-select-desc">{selected.description}</span>
          )}
        </span>
        <svg className="glass-select-chevron" width="14" height="14" viewBox="0 0 12 12" aria-hidden="true">
          <path fill="currentColor" d="M6 8L2 4h8z" />
        </svg>
      </button>

      {open && createPortal(menu, document.body)}
    </div>
  );
}

export default GlassSelect;
