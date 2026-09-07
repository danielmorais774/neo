// Bloco de código do chat: barra de topo (linguagem + copiar) e, abaixo, o
// <pre> que rola. Substitui o <pre> que o ReactMarkdown geraria (ver
// MessageBubble).
//
// A barra existe por dois motivos: um botão flutuando SOBRE o código ficava por
// cima do texto (uma chave ssh é uma linha só, longa — não há canto vazio pra
// ocupar), e o <pre> tem overflow-x: auto, então um botão dentro dele rolaria
// junto com o conteúdo.

import { useCallback, useEffect, useRef, useState } from 'react';
import { isValidElement } from 'react';
import type { ReactNode } from 'react';
import { copyToClipboard } from '../lib/clipboard';
import './CodeBlock.css';

type CopyState = 'idle' | 'copied' | 'error';

const LABEL: Record<CopyState, string> = {
  idle: 'Copiar',
  copied: 'Copiado',
  error: 'Falhou',
};

interface CopyButtonProps {
  /** Lê o texto na hora do clique (o conteúdo pode mudar durante o streaming). */
  getText: () => string;
  className?: string;
}

export function CopyButton({ getText, className }: CopyButtonProps) {
  const [state, setState] = useState<CopyState>('idle');
  const resetTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  const handleCopy = useCallback(async () => {
    const text = getText();
    if (!text) return;
    const ok = await copyToClipboard(text);
    setState(ok ? 'copied' : 'error');
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setState('idle'), 1800);
  }, [getText]);

  return (
    <button
      type="button"
      className={`code-copy-button ${state !== 'idle' ? `is-${state}` : ''} ${className ?? ''}`.trim()}
      onClick={handleCopy}
      title={state === 'idle' ? 'Copiar para a área de transferência' : LABEL[state]}
      aria-label={LABEL[state]}
    >
      {state === 'copied' ? (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="11" height="11" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
      <span className="code-copy-label">{LABEL[state]}</span>
    </button>
  );
}

/**
 * Linguagem da cerca ```lang. O markdown põe `language-lang` no <code> filho,
 * não no <pre> — daí olhar o children em vez das props daqui.
 */
function languageOf(children: ReactNode): string {
  if (!isValidElement<{ className?: string }>(children)) return '';
  const match = /language-([\w+-]+)/.exec(children.props.className ?? '');
  return match ? match[1] : '';
}

interface CodeBlockProps {
  children?: ReactNode;
  className?: string;
}

function CodeBlock({ children, className }: CodeBlockProps) {
  const preRef = useRef<HTMLPreElement>(null);

  // O texto sai do DOM, não dos children do React: o rehype-highlight quebra o
  // código em dezenas de <span> coloridos, e remontar a string a partir da
  // árvore React erraria espaços/quebras.
  const getText = useCallback(() => preRef.current?.textContent ?? '', []);
  const language = languageOf(children);

  return (
    <div className="code-block">
      <div className="code-block-bar">
        <span className="code-block-lang">{language}</span>
        <CopyButton getText={getText} />
      </div>
      <pre ref={preRef} className={`code-block-pre ${className ?? ''}`.trim()}>
        {children}
      </pre>
    </div>
  );
}

export default CodeBlock;
