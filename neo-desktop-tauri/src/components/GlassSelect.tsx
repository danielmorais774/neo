import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import './GlassSelect.css';

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
      setOpen(false);
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };

    window.addEventListener('mousedown', onMouseDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('mousedown', onMouseDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  // Reposition portal menu on open / resize / scroll
  useEffect(() => {
    if (!open) {
      setMenuStyle(null);
      return;
    }

    const updatePosition = () => {
      const rootEl = rootRef.current;
      if (!rootEl) return;
      const rect = rootEl.getBoundingClientRect();

      // Estimate menu height and width
      const estItemH = 44; // estimate per option
      const estHeight = Math.min(400, options.length * estItemH + 12);
      const menuWidth = Math.min(320, Math.max(210, rect.width));

      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      const showBelow = spaceBelow >= estHeight || spaceBelow >= spaceAbove;

      const left = Math.max(8, rect.left);
      const top = showBelow ? rect.bottom + 8 : Math.max(8, rect.top - estHeight - 8);

      setMenuStyle({ position: 'fixed', left: `${left}px`, top: `${top}px`, width: `${menuWidth}px`, zIndex: 9999 });
    };

    updatePosition();
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', updatePosition, true);
    return () => {
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', updatePosition, true);
    };
  }, [open, options.length]);

  const handleSelect = (next: T) => {
    onChange(next);
    setOpen(false);
  };

  const menu = (
    <div
      ref={menuRef}
      className="glass-select-menu portal"
      role="listbox"
      aria-label={ariaLabel}
      style={menuStyle || undefined}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="option"
          aria-selected={opt.value === value}
          className={`glass-select-option ${opt.value === value ? 'selected' : ''}`}
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
        onClick={() => setOpen(v => !v)}
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
