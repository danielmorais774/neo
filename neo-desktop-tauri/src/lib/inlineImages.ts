// Imagens inline no chat: o núcleo cognitivo pede a exibição escrevendo
// `[[IMAGE:origem]]` na resposta (ver _INLINE_IMAGE_INSTRUCTION no neo-core) e é
// AQUI que o marcador sai do texto e vira imagem de verdade na bolha.
//
// `origem` é um caminho do disco do usuário (com espaços, sem aspas),
// `file:<file_id>` de um arquivo do catálogo do neo-core, ou uma URL http(s)
// (foto de anúncio numa busca de produto). Legenda opcional após `|`. Os BYTES
// nunca passam pelo prompt: quem lê/baixa é o Rust (load_chat_image), direto
// pro <img> como data URL.
//
// Por que marcador e não `![](markdown)`: caminho real tem espaço
// (`WhatsApp Image 2026-07-29 at 17.22.32.jpeg`), e em markdown isso exige
// `<...>` que o modelo esquece — o token `[[…]]` também é a convenção que o
// resto do sistema já usa ([[VOICE:…]], [[HANDOFF:…]]).

export interface InlineImageRef {
  /** Conteúdo cru do marcador: caminho local, `file:<file_id>` ou URL http(s). */
  source: string;
  /** Legenda opcional escrita após `|`. */
  caption?: string;
  /** É um arquivo do catálogo do neo-core (não do disco do usuário)? */
  fromCatalog: boolean;
  /** É uma URL http(s) — o Rust baixa os bytes e devolve data URL. */
  fromRemote: boolean;
}

export interface ExtractedInlineImages {
  /** Texto sem os marcadores, pronto para o markdown. */
  text: string;
  /** Imagens pedidas, na ordem em que apareceram (sem repetir a mesma origem). */
  images: InlineImageRef[];
}

// `[[IMAGE: … ]]` — tolerante a caixa e a espaços em volta de IMAGE/`:`. O corpo
// para no primeiro `]` para não engolir o resto da resposta se o modelo esquecer
// de fechar (caminho com `]` é raro; resposta truncada, não).
const MARKER_RE = /\[\[\s*IMAGE\s*:\s*([^\]]*?)\s*\]\]/gi;

// Marcador aberto e nunca fechado (resposta cortada no meio): tem que sair do
// texto também, senão o usuário lê meio caminho de arquivo na bolha.
const DANGLING_RE = /\[\[\s*IMAGE\s*:[^\]]*$/i;

function parseMarkerBody(body: string): InlineImageRef | null {
  const raw = (body || '').trim();
  if (!raw) return null;
  const pipe = raw.indexOf('|');
  const source = (pipe >= 0 ? raw.slice(0, pipe) : raw).trim().replace(/^["']|["']$/g, '');
  const caption = pipe >= 0 ? raw.slice(pipe + 1).trim() : '';
  if (!source) return null;
  return {
    source,
    caption: caption || undefined,
    fromCatalog: /^file:/i.test(source),
    fromRemote: /^https?:\/\//i.test(source),
  };
}

/**
 * Separa `content` em texto exibível + imagens pedidas pelo marcador.
 *
 * Mensagem sem marcador volta byte a byte (nunca mexe numa resposta normal).
 */
export function extractInlineImages(content: string): ExtractedInlineImages {
  if (!content || !content.includes('[[')) return { text: content, images: [] };

  const images: InlineImageRef[] = [];
  const seen = new Set<string>();
  let text = content.replace(MARKER_RE, (_all, body: string) => {
    const ref = parseMarkerBody(body);
    if (ref && !seen.has(ref.source)) {
      seen.add(ref.source);
      images.push(ref);
    }
    return '';
  });

  if (!images.length && !DANGLING_RE.test(text)) {
    return { text: content, images: [] };
  }

  text = text.replace(DANGLING_RE, '');
  // Não deixa o buraco do marcador virar linha vazia/parágrafo solto.
  text = text
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .filter((line, idx, arr) => line.trim() !== '' || (idx > 0 && arr[idx - 1].trim() !== ''))
    .join('\n')
    .trim();

  return { text, images };
}

/** Só o texto, sem marcadores — para TTS/legenda (o marcador não é falado). */
export function stripInlineImageMarkers(content: string): string {
  return extractInlineImages(content).text;
}
