// Arquivo que já está no disco do usuário: o núcleo cognitivo escreve
// `[[FILE:/caminho/absoluto]]` (ver _LOCAL_FILE_INSTRUCTION no neo-core) e a bolha
// troca o marcador por um cartão com nome, tamanho e "Mostrar no Finder".
//
// É o par do botão de download do catálogo: aquele traz um arquivo GERADO NO
// SANDBOX para a máquina; este aponta um arquivo que JÁ ESTÁ nela (o vídeo do
// YouTube baixado pelo yt-dlp do Mac, session-3ed86282). Legenda opcional após `|`.

import { codeLookup } from './codeSpans';

export interface LocalFileRef {
  /** Caminho absoluto (ou `~/…`) no disco do usuário. */
  path: string;
  /** Legenda opcional escrita após `|`. */
  caption?: string;
}

export interface ExtractedLocalFiles {
  /** Texto sem os marcadores, pronto para o markdown. */
  text: string;
  /** Arquivos citados, na ordem, sem repetir o mesmo caminho. */
  files: LocalFileRef[];
}

// Caixa e espaços em volta de FILE/`:` tolerados, como no `[[IMAGE:…]]`. A
// diferença: o corpo vai até o `]]`, não até o primeiro `]`, porque o yt-dlp põe
// o id entre colchetes no nome (`taca-lê pau … [nRuiacgrY0w].mp4`). Em troca,
// o corpo não atravessa linha: um marcador sem fechamento morre no fim da
// linha em vez de engolir o resto da resposta.
const BODY = String.raw`((?:[^\]\n]|\](?!\]))*?)`;
const MARKER_RE = new RegExp(String.raw`\[\[\s*FILE\s*:\s*` + BODY + String.raw`\s*\]\]`, 'gi');
const DANGLING_RE = /\[\[\s*FILE\s*:(?:[^\]\n]|\](?!\]))*$/gim;

/** Caminho do sandbox do cérebro (container na VPS), não do disco do usuário. */
export function isSandboxPath(path: string): boolean {
  return /^\/workspace(\/|$)/.test(path.trim());
}

/** Caminho absoluto ou `~/…`: o único formato que o cartão sabe conferir no disco. */
export function isLocalPath(path: string): boolean {
  return /^(\/|~\/)/.test(path.trim());
}

function parseMarkerBody(body: string): LocalFileRef | null {
  const raw = (body || '').trim();
  if (!raw) return null;
  const pipe = raw.indexOf('|');
  const path = (pipe >= 0 ? raw.slice(0, pipe) : raw).trim().replace(/^["'`]|["'`]$/g, '');
  const caption = pipe >= 0 ? raw.slice(pipe + 1).trim() : '';
  if (!path) return null;
  return { path, caption: caption || undefined };
}

/**
 * Separa `content` em texto exibível + arquivos locais citados pelo marcador.
 *
 * Mensagem sem marcador volta byte a byte.
 */
export function extractLocalFiles(content: string): ExtractedLocalFiles {
  if (!content || !content.includes('[[')) return { text: content, files: [] };

  // Marcador que começa dentro de código (`inline` ou bloco) é citação: fica no
  // texto como o modelo escreveu (ver lib/codeSpans.ts).
  const inCode = codeLookup(content);
  const files: LocalFileRef[] = [];
  const seen = new Set<string>();
  let removed = false;
  let text = content.replace(MARKER_RE, (all: string, body: string, offset: number) => {
    if (inCode(offset)) return all;
    removed = true;
    const ref = parseMarkerBody(body);
    // `/workspace/…` é o sandbox da VPS: nunca existe neste disco, e o arquivo
    // já chega pelo botão de download do catálogo. Sem cartão (seria sempre
    // "Não achei o arquivo"); o neo-core já tira o marcador, isto cobre o
    // histórico salvo antes disso.
    // Idem para o que nem é caminho (o `[[FILE:<file_id>]]` da session-b4708de0).
    if (ref && (isSandboxPath(ref.path) || !isLocalPath(ref.path))) return '';
    if (ref && !seen.has(ref.path)) {
      seen.add(ref.path);
      files.push(ref);
    }
    return '';
  });

  const inCodeNow = removed ? codeLookup(text) : inCode;
  let dangling = false;
  text = text.replace(DANGLING_RE, (all: string, offset: number) => {
    if (inCodeNow(offset)) return all;
    dangling = true;
    return '';
  });

  if (!removed && !dangling) {
    return { text: content, files: [] };
  }

  text = text
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .filter((line, idx, arr) => line.trim() !== '' || (idx > 0 && arr[idx - 1].trim() !== ''))
    .join('\n')
    .trim();

  return { text, files };
}

/** Só o texto, sem marcadores — para TTS/legenda (o caminho não é falado). */
export function stripLocalFileMarkers(content: string): string {
  return extractLocalFiles(content).text;
}

/** "Finder" no Mac, "Explorer" no Windows: o nome que a pessoa reconhece. */
export function fileManagerName(platform: string = navigator.platform || ''): string {
  const p = platform.toLowerCase();
  if (p.includes('mac')) return 'Finder';
  if (p.includes('win')) return 'Explorer';
  return 'gerenciador de arquivos';
}
