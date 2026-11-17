// Cartão do arquivo que já está no disco do usuário (`[[FILE:…]]`): nome,
// tamanho e "Mostrar no Finder". O Rust confere que o arquivo existe antes de
// desenhar (stat_local_file); caminho inventado vira cartão de erro VISÍVEL,
// como no `[[IMAGE:…]]`.
//
// Só "mostrar na pasta", sem "abrir": o caminho é escrito pelo modelo, e abrir
// com o app padrão executaria um `.command`/`.sh` que ele apontasse. Revelar no
// Finder não executa nada; abrir fica a um duplo clique, com a pessoa vendo.

import { useEffect, useState } from 'react';
import type { LocalFileInfo } from '../lib/tauriApi';
import { fileManagerName, type LocalFileRef } from '../lib/localFiles';
import './LocalFiles.css';

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; info: LocalFileInfo }
  | { status: 'error'; message: string };

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

const ICONES: Array<[RegExp, string]> = [
  [/\.(mp4|mov|mkv|webm|avi|m4v)$/i, '🎬'],
  [/\.(mp3|m4a|wav|flac|ogg|opus|aac)$/i, '🎵'],
  [/\.(png|jpe?g|gif|webp|heic|bmp|svg)$/i, '🖼'],
  [/\.(zip|rar|7z|tar|gz|xz|dmg)$/i, '🗜'],
  [/\.pdf$/i, '📕'],
];

function iconFor(name: string): string {
  return ICONES.find(([re]) => re.test(name))?.[1] ?? '📄';
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function useLocalFile(path: string): LoadState {
  // Mesmo padrão do useChatImage: "loading" é derivado da troca de caminho.
  const [entry, setEntry] = useState<{ path: string; state: LoadState }>({
    path,
    state: { status: 'loading' },
  });

  useEffect(() => {
    let alive = true;
    const api = window.electronAPI;
    const load = api?.statLocalFile
      ? api.statLocalFile(path)
      : Promise.reject(new Error('Este app não sabe mostrar arquivos locais (build antigo).'));
    load
      .then((info) => {
        if (alive) setEntry({ path, state: { status: 'ready', info } });
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        if (alive) setEntry({ path, state: { status: 'error', message } });
      });
    return () => {
      alive = false;
    };
  }, [path]);

  return entry.path === path ? entry.state : { status: 'loading' };
}

function LocalFileCard({ file }: { file: LocalFileRef }) {
  const state = useLocalFile(file.path);
  const [revealError, setRevealError] = useState<string | null>(null);
  const gerenciador = fileManagerName();

  if (state.status === 'error') {
    return (
      <div className="local-file-card local-file-error">
        <span className="local-file-icon">⚠</span>
        <div className="local-file-body">
          <span className="local-file-name">Não achei o arquivo</span>
          <span className="local-file-detail">{state.message}</span>
          <code className="local-file-path">{file.path}</code>
        </div>
      </div>
    );
  }

  const name = state.status === 'ready' ? state.info.name : baseName(file.path);
  const realPath = state.status === 'ready' ? state.info.path : file.path;

  const reveal = () => {
    setRevealError(null);
    window.electronAPI
      ?.revealInFolder?.(realPath)
      .catch((err: unknown) => setRevealError(err instanceof Error ? err.message : String(err)));
  };

  return (
    <div className="local-file-card" title={realPath}>
      <span className="local-file-icon">{iconFor(name)}</span>
      <div className="local-file-body">
        <span className="local-file-name">{file.caption || name}</span>
        <span className="local-file-detail">
          {state.status === 'ready' ? humanSize(state.info.sizeBytes) : 'conferindo…'}
          {' · '}
          <span className="local-file-dir">{realPath}</span>
        </span>
        {revealError && <span className="local-file-detail local-file-reveal-error">{revealError}</span>}
      </div>
      <button
        type="button"
        className="local-file-reveal"
        disabled={state.status !== 'ready'}
        onClick={reveal}
      >
        Mostrar no {gerenciador}
      </button>
    </div>
  );
}

function LocalFiles({ files }: { files: LocalFileRef[] }) {
  if (!files.length) return null;
  return (
    <div className="local-file-list">
      {files.map((f) => (
        <LocalFileCard key={f.path} file={f} />
      ))}
    </div>
  );
}

export default LocalFiles;
