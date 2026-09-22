/*
 * Desenho do grafo em canvas, com cara de tecido neural.
 *
 * Duas decisões carregam o desempenho:
 *
 * 1. Os brilhos são SPRITES pré-renderizados (um por cor de família), não
 *    createRadialGradient por nó por quadro. Com 500 nós a 60 fps a diferença é
 *    entre 30 mil gradientes por segundo e seis imagens desenhadas com escala.
 * 2. Aresta normal sai numa cor chapada, misturada entre as duas pontas.
 *    Gradiente de verdade só nas arestas em destaque, que são poucas.
 *
 * O React não participa: nada aqui causa render. O canvas é um nó não
 * controlado, e hover/zoom/arraste mexem em refs.
 */

import type { SimNode, SimEdge } from './simulation';
import { mix, rgba } from './palette';

export interface Camera {
  x: number;
  y: number;
  scale: number;
}

export interface DrawState {
  time: number;             // segundos desde a montagem
  camera: Camera;
  /** devicePixelRatio: entra nas duas matrizes, nunca num scale() à parte —
   *  o render troca de matriz várias vezes e um scale solto se perderia. */
  dpr: number;
  hovered: SimNode | null;
  selected: string | null;
  /** Nós em foco. Vazio = sem foco, tudo com brilho cheio. */
  focus: Set<string> | null;
  hidden: Set<string>;      // ids escondidos por filtro de família/tipo
  colorOf: (node: SimNode) => string;
  labelOf: (node: SimNode) => string;
  showPulses: boolean;
  showLabels: boolean;
}

const DIM = 0.09;
const MAX_LABELS = 70;
const MAX_CHARS = 26;

/** Nomes do PKS chegam como frases inteiras ("Feedback: consultas de verificação
 *  muito rápidas retornam estado antigo"). Inteiro na tela não cabe. */
const clip = (s: string) => (s.length > MAX_CHARS ? `${s.slice(0, MAX_CHARS - 1)}…` : s);

/** Brilho radial pré-renderizado, uma vez por cor. */
function makeGlow(color: string): HTMLCanvasElement {
  const S = 128;
  const c = document.createElement('canvas');
  c.width = c.height = S;
  const g = c.getContext('2d')!;
  const grad = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
  grad.addColorStop(0, rgba(color, 0.55));
  grad.addColorStop(0.25, rgba(color, 0.22));
  grad.addColorStop(0.55, rgba(color, 0.05));
  grad.addColorStop(1, rgba(color, 0));
  g.fillStyle = grad;
  g.fillRect(0, 0, S, S);
  return c;
}

/** Corpo celular: núcleo quase branco abrindo pra cor da família. */
function makeSoma(color: string): HTMLCanvasElement {
  const S = 96;
  const c = document.createElement('canvas');
  c.width = c.height = S;
  const g = c.getContext('2d')!;
  const grad = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
  grad.addColorStop(0, 'rgba(255,255,255,0.95)');
  grad.addColorStop(0.32, rgba(color, 0.98));
  grad.addColorStop(0.82, rgba(color, 0.72));
  grad.addColorStop(1, rgba(color, 0));
  g.fillStyle = grad;
  g.beginPath();
  g.arc(S / 2, S / 2, S / 2, 0, Math.PI * 2);
  g.fill();
  return c;
}

export class NeuralRenderer {
  private glow = new Map<string, HTMLCanvasElement>();
  private soma = new Map<string, HTMLCanvasElement>();

  /* measureText é caro e o texto aqui se repete quadro a quadro. A largura é
   * linear no tamanho da fonte, então basta guardar a largura por pixel. */
  private widths = new Map<string, number>();
  private textWidth(ctx: CanvasRenderingContext2D, text: string, fontPx: number) {
    let per = this.widths.get(text);
    if (per === undefined) {
      per = ctx.measureText(text).width / fontPx;
      this.widths.set(text, per);
    }
    return per * fontPx;
  }

  private sprite(cache: Map<string, HTMLCanvasElement>, color: string, make: (c: string) => HTMLCanvasElement) {
    let s = cache.get(color);
    if (!s) {
      s = make(color);
      cache.set(color, s);
    }
    return s;
  }

  draw(
    ctx: CanvasRenderingContext2D,
    width: number,
    height: number,
    nodes: SimNode[],
    edges: SimEdge[],
    st: DrawState,
  ) {
    const { camera: cam, time, dpr } = st;
    const screen = () => ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const world = () =>
      ctx.setTransform(cam.scale * dpr, 0, 0, cam.scale * dpr, cam.x * dpr, cam.y * dpr);

    screen();
    ctx.clearRect(0, 0, width, height);
    this.drawBackdrop(ctx, width, height, time);

    world();

    // Retângulo visível em coordenadas do mundo, com folga pro brilho.
    const pad = 160 / cam.scale;
    const view = {
      minX: -cam.x / cam.scale - pad,
      minY: -cam.y / cam.scale - pad,
      maxX: (width - cam.x) / cam.scale + pad,
      maxY: (height - cam.y) / cam.scale + pad,
    };
    const visible = (n: SimNode) =>
      n.x > view.minX && n.x < view.maxX && n.y > view.minY && n.y < view.maxY;

    const alphaOf = (id: string) => (!st.focus || st.focus.has(id) ? 1 : DIM);

    // ── Dendritos ──────────────────────────────────────────────────────────
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineCap = 'round';
    for (const e of edges) {
      if (st.hidden.has(e.a.id) || st.hidden.has(e.b.id)) continue;
      if (!visible(e.a) && !visible(e.b)) continue;
      const lit = Math.min(alphaOf(e.a.id), alphaOf(e.b.id));
      const ca = st.colorOf(e.a);
      const cb = st.colorOf(e.b);
      const { cx, cy } = control(e);

      ctx.beginPath();
      ctx.moveTo(e.a.x, e.a.y);
      ctx.quadraticCurveTo(cx, cy, e.b.x, e.b.y);

      if (lit === 1 && st.focus) {
        // Poucas arestas: aí vale o gradiente de verdade, ponta a ponta.
        const g = ctx.createLinearGradient(e.a.x, e.a.y, e.b.x, e.b.y);
        g.addColorStop(0, rgba(ca, 0.75));
        g.addColorStop(1, rgba(cb, 0.75));
        ctx.strokeStyle = g;
        ctx.lineWidth = e.w * 1.8;
      } else {
        ctx.strokeStyle = mix(ca, cb, 0.3 * lit);
        ctx.lineWidth = e.w;
      }
      ctx.stroke();
    }

    // ── Pulsos sinápticos ──────────────────────────────────────────────────
    if (st.showPulses) {
      for (const e of edges) {
        if (st.hidden.has(e.a.id) || st.hidden.has(e.b.id)) continue;
        if (!visible(e.a) && !visible(e.b)) continue;
        const lit = Math.min(alphaOf(e.a.id), alphaOf(e.b.id));
        if (lit < 1 && st.focus) continue;   // sem foco não há pulso: menos ruído
        const boost = st.focus ? 2.2 : 1;
        const t = (time * e.speed * boost + e.offset) % 1;
        const { cx, cy } = control(e);
        const color = st.colorOf(e.a);
        // Cometa: a cabeça mais o rastro em três amostras atrás.
        for (let k = 0; k < 4; k++) {
          const tt = t - k * 0.035;
          if (tt < 0) continue;
          const p = onQuad(e.a, cx, cy, e.b, tt);
          const fade = (1 - k / 4) * (1 - Math.abs(t - 0.5) * 0.5);
          ctx.beginPath();
          ctx.arc(p.x, p.y, (2.2 - k * 0.4) * (st.focus ? 1.35 : 1), 0, Math.PI * 2);
          ctx.fillStyle = k === 0 ? `rgba(255,255,255,${0.85 * fade})` : rgba(color, 0.5 * fade);
          ctx.fill();
        }
      }
    }

    // ── Somas ──────────────────────────────────────────────────────────────
    for (const n of nodes) {
      if (st.hidden.has(n.id) || !visible(n)) continue;
      const a = alphaOf(n.id);
      const color = st.colorOf(n);
      // Respiro: cada nó tem fase própria, então a rede inteira "pisca" fora
      // de compasso, como atividade de fundo.
      const breath = 0.82 + 0.18 * Math.sin(time * 1.5 + n.phase);
      const isHot = st.hovered?.id === n.id || st.selected === n.id;
      const g = this.sprite(this.glow, color, makeGlow);
      const gs = n.r * (isHot ? 11 : 7.5) * breath;
      ctx.globalAlpha = a * (isHot ? 1 : 0.85);
      ctx.drawImage(g, n.x - gs / 2, n.y - gs / 2, gs, gs);
    }
    ctx.globalAlpha = 1;

    ctx.globalCompositeOperation = 'source-over';
    for (const n of nodes) {
      if (st.hidden.has(n.id) || !visible(n)) continue;
      const a = alphaOf(n.id);
      const color = st.colorOf(n);
      const breath = 0.94 + 0.06 * Math.sin(time * 1.5 + n.phase);
      const isHot = st.hovered?.id === n.id || st.selected === n.id;
      const r = n.r * breath * (isHot ? 1.18 : 1);

      ctx.globalAlpha = a;
      const s = this.sprite(this.soma, color, makeSoma);
      const ss = r * 2.6;
      ctx.drawImage(s, n.x - ss / 2, n.y - ss / 2, ss, ss);

      // Membrana
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = isHot ? 'rgba(255,255,255,0.9)' : rgba(color, 0.55);
      ctx.lineWidth = isHot ? 1.6 / cam.scale + 0.6 : 0.8;
      ctx.stroke();

      if (st.selected === n.id) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, r + 8 + Math.sin(time * 3) * 2, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255,255,255,0.35)';
        ctx.lineWidth = 1 / cam.scale;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    // ── Rótulos ────────────────────────────────────────────────────────────
    // Em coordenadas de TELA, tamanho fixo, e só os que cabem sem encostar.
    //
    // O painel antigo desenhava os 103 nomes e as 91 legendas de aresta em
    // qualquer zoom, escalados junto com o mundo: de longe virava um borrão
    // cinza, de perto saíam gigantes. Aqui o nome tem sempre 11 px e um nome
    // só entra se a caixa dele não bater em nenhuma já colocada — quem perde a
    // disputa é sempre o nó de menor grau.
    screen();
    // Caixas já ocupadas: nomes e tipos de relação disputam o MESMO espaço.
    const placed: number[][] = [];
    const fits = (box: number[]) => {
      for (const q of placed) {
        if (box[0] < q[2] && box[2] > q[0] && box[1] < q[3] && box[3] > q[1]) return false;
      }
      return true;
    };

    if (st.showLabels || st.focus || st.hovered) {
      const FONT = 11;
      ctx.font = `500 ${FONT}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';

      const candidates: { n: SimNode; prio: number }[] = [];
      for (const n of nodes) {
        if (st.hidden.has(n.id) || !visible(n)) continue;
        const inFocus = !st.focus || st.focus.has(n.id);
        // Sem "Nomes" ligado só aparecem os do foco — é o modo limpo.
        if (!st.showLabels && !st.focus && st.hovered?.id !== n.id) continue;
        if (st.focus && !inFocus) continue;
        const hot = st.hovered?.id === n.id || st.selected === n.id;
        candidates.push({ n, prio: hot ? 1e6 : n.degree * 100 + n.r });
      }
      candidates.sort((a, b) => b.prio - a.prio);

      let drawn = 0;
      for (const { n } of candidates) {
        if (drawn >= MAX_LABELS) break;
        const text = clip(st.labelOf(n));
        const w = this.textWidth(ctx, text, FONT);
        const sx = n.x * cam.scale + cam.x;
        const sy = n.y * cam.scale + cam.y + n.r * cam.scale + 5;
        if (sx < -w || sx > width + w || sy < -20 || sy > height) continue;
        const box = [sx - w / 2 - 3, sy - 2, sx + w / 2 + 3, sy + FONT + 2];
        if (!fits(box)) continue;
        placed.push(box);
        drawn++;

        ctx.lineWidth = 3;
        ctx.strokeStyle = 'rgba(4,4,8,0.9)';
        ctx.strokeText(text, sx, sy);
        ctx.fillStyle = 'rgba(241,240,246,0.9)';
        ctx.fillText(text, sx, sy);
      }
    }

    // Tipo da relação só aparece nas arestas do nó sob o cursor — é ali que a
    // pergunta "que relação é essa?" existe.
    //
    // E passa pela mesma disputa de espaço: num hub de grau 30, escrever os 30
    // tipos dava um novelo de "owns" empilhado em cima do próprio soma. Aqui
    // ganha quem tem aresta mais longa na tela, que é justamente onde o texto
    // cabe sem cobrir nada.
    if (st.hovered) {
      const EFONT = 10;
      ctx.font = `500 ${EFONT}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      const touching = edges
        .filter(
          (e) =>
            (e.a.id === st.hovered!.id || e.b.id === st.hovered!.id) &&
            !st.hidden.has(e.a.id) &&
            !st.hidden.has(e.b.id),
        )
        .map((e) => ({ e, len: Math.hypot(e.b.x - e.a.x, e.b.y - e.a.y) * cam.scale }))
        .sort((x, y) => y.len - x.len);

      for (const { e, len } of touching) {
        // Aresta curta na tela não tem onde pôr texto: o rótulo sairia por
        // cima dos dois somas.
        if (len < 46) continue;
        const { cx, cy } = control(e);
        const p = onQuad(e.a, cx, cy, e.b, 0.5);
        const sx = p.x * cam.scale + cam.x;
        const sy = p.y * cam.scale + cam.y;
        const w = this.textWidth(ctx, e.type, EFONT);
        const box = [sx - w / 2 - 3, sy - EFONT / 2 - 2, sx + w / 2 + 3, sy + EFONT / 2 + 2];
        if (!fits(box)) continue;
        placed.push(box);

        ctx.lineWidth = 3;
        ctx.strokeStyle = 'rgba(4,4,8,0.92)';
        ctx.strokeText(e.type, sx, sy);
        ctx.fillStyle = 'rgba(241,240,246,0.75)';
        ctx.fillText(e.type, sx, sy);
      }
    }

    screen();
  }

  /** Fundo: gradiente do painel mais uma respiração lenta, pra não ficar um
   *  retângulo preto morto atrás da rede. */
  private drawBackdrop(ctx: CanvasRenderingContext2D, w: number, h: number, time: number) {
    ctx.fillStyle = '#06060b';
    ctx.fillRect(0, 0, w, h);
    const pulse = 0.5 + 0.5 * Math.sin(time * 0.25);
    const g = ctx.createRadialGradient(w * 0.5, h * 0.45, 0, w * 0.5, h * 0.45, Math.max(w, h) * 0.7);
    g.addColorStop(0, `rgba(124,58,237,${0.07 + pulse * 0.03})`);
    g.addColorStop(0.5, 'rgba(19,146,212,0.025)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);
  }
}

// ── Geometria do dendrito ────────────────────────────────────────────────────

function control(e: SimEdge) {
  const dx = e.b.x - e.a.x;
  const dy = e.b.y - e.a.y;
  return {
    cx: (e.a.x + e.b.x) / 2 - dy * e.bow,
    cy: (e.a.y + e.b.y) / 2 + dx * e.bow,
  };
}

function onQuad(a: { x: number; y: number }, cx: number, cy: number, b: { x: number; y: number }, t: number) {
  const u = 1 - t;
  return {
    x: u * u * a.x + 2 * u * t * cx + t * t * b.x,
    y: u * u * a.y + 2 * u * t * cy + t * t * b.y,
  };
}

export { control as edgeControl, onQuad as pointOnEdge };
