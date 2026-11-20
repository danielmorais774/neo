// Trechos de CÓDIGO do markdown (`inline` e blocos ``` / ~~~) que os extratores
// de marcador (`[[IMAGE:…]]`, `[[FILE:…]]`) não podem tocar.
//
// Por quê: quando o modelo CITA o marcador como exemplo ("entregar com file_save
// + marcador `[[IMAGE:file:<file_id>]]`"), o texto entre crases é literal — não é
// pedido de imagem. Sem isto o extrator tirava o marcador de dentro das crases
// (a bolha mostrava "marcador ``") e tentava baixar `file:<file_id>` (cartão
// vermelho "Download falhou: 404", gravação do PDF Industry 5.0, 19/11).
//
// Conta onde o marcador COMEÇA: `[[FILE:`/caminho`]]` (crase dentro do
// marcador, que o parser de FILE já tira) continua valendo.

type Range = [start: number, end: number];

const FENCE_OPEN_RE = /^ {0,3}(`{3,}|~{3,})/;

/** Fim da linha que começa em `pos` (índice do `\n`, ou o fim do texto). */
function lineEnd(text: string, pos: number): number {
  const nl = text.indexOf('\n', pos);
  return nl < 0 ? text.length : nl;
}

/** Spans `inline` dentro de [from, to): corrida de N crases fecha na próxima de N. */
function inlineRanges(text: string, from: number, to: number, out: Range[]): void {
  let i = from;
  while (i < to) {
    if (text[i] !== '`') {
      i += 1;
      continue;
    }
    let n = 0;
    while (i + n < to && text[i + n] === '`') n += 1;
    let j = i + n;
    let closed = -1;
    while (j < to) {
      if (text[j] !== '`') {
        j += 1;
        continue;
      }
      let m = 0;
      while (j + m < to && text[j + m] === '`') m += 1;
      if (m === n) {
        closed = j + m;
        break;
      }
      j += m;
    }
    if (closed < 0) {
      // Crase sem par é literal (ou a resposta ainda está chegando).
      i += n;
      continue;
    }
    out.push([i, closed]);
    i = closed;
  }
}

/** Trechos de código do markdown, em ordem e sem sobreposição. */
export function findCodeRanges(text: string): Range[] {
  const ranges: Range[] = [];
  let pos = 0;
  let proseStart = 0;
  while (pos < text.length) {
    const end = lineEnd(text, pos);
    const line = text.slice(pos, end);
    const open = FENCE_OPEN_RE.exec(line);
    // "```x```" numa linha só é código inline: cerca de crase não tem crase no info.
    if (!open || (open[1][0] === '`' && line.slice(open[0].length).includes('`'))) {
      pos = end + 1;
      continue;
    }
    inlineRanges(text, proseStart, pos, ranges);
    const fence = open[1];
    const closeRe = new RegExp(String.raw`^ {0,3}` + (fence[0] === '`' ? '`' : '~') + `{${fence.length},}[ \\t]*$`);
    let blockEnd = text.length; // bloco sem fechamento vai até o fim (CommonMark)
    let scan = end + 1;
    while (scan < text.length) {
      const e = lineEnd(text, scan);
      if (closeRe.test(text.slice(scan, e))) {
        blockEnd = e;
        break;
      }
      scan = e + 1;
    }
    ranges.push([pos, blockEnd]);
    pos = blockEnd + 1;
    proseStart = Math.min(pos, text.length);
  }
  inlineRanges(text, proseStart, text.length, ranges);
  return ranges;
}

/** `offset` cai dentro de um trecho de código de `text`? */
export function codeLookup(text: string): (offset: number) => boolean {
  if (!text.includes('`') && !text.includes('~~~')) return () => false;
  const ranges = findCodeRanges(text);
  if (!ranges.length) return () => false;
  return (offset) => ranges.some(([start, end]) => offset >= start && offset < end);
}
