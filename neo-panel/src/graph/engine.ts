/*
 * Cola entre o canvas, a simulação e o render: um só requestAnimationFrame,
 * câmera, e os eventos de ponteiro.
 *
 * Tudo que acontece a 60 Hz mora aqui, fora do React. O painel antigo chamava
 * setPositions(new Map(...)) a cada mousemove de arraste e setTransform a cada
 * mousemove de pan — cada um re-renderizava os 103 grupos <g> de nó e as 91
 * arestas com <text>.
 *
 * Medido em 2026-09-19 arrastando um nó por 1,5 s, com os mesmos dados nas duas
 * versões (p95 do intervalo entre quadros; a mediana engana porque quando a
 * thread trava o rAF vem em rajada):
 *
 *   CPU à vontade   antes p95 20,8 ms / pior 63 ms   →  depois 17,1 / 19
 *   CPU 4× mais devagar  antes p95 90,5 ms / pior 96 ms   →  depois 19,2 / 31
 *   CPU 6× mais devagar  antes p95  138 ms / pior 150 ms  →  depois 20,1 / 50
 *
 * Ou seja: numa máquina ociosa os dois seguram 60 fps, e o engasgo aparece
 * exatamente quando a máquina está ocupada — que é como esta aqui vive.
 */

import { Simulation, type SimNode } from './simulation';
import { NeuralRenderer, type Camera } from './renderer';

export interface EngineEntity {
  id: string;
  type: string;
  name: string;
  confidence: number;
}
export interface EngineRelation {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
  confidence: number;
}

export interface EngineOptions {
  showPulses: boolean;
  showLabels: boolean;
}

export interface EngineCallbacks {
  onHover: (id: string | null) => void;
  onSelect: (id: string | null) => void;
  /** Avisa a UI quando a rede terminou de se acomodar (some o "acomodando…"). */
  onSettle: (settled: boolean) => void;
}

const MIN_SCALE = 0.08;
const MAX_SCALE = 5;

export class GraphEngine {
  readonly sim = new Simulation();
  private renderer = new NeuralRenderer();
  private ctx: CanvasRenderingContext2D;
  private raf = 0;
  private t0 = performance.now();

  private cam: Camera = { x: 0, y: 0, scale: 1 };
  private autoFit = true;
  private w = 1;
  private h = 1;
  private dpr = 1;

  private pointer: { x: number; y: number } | null = null;   // tela
  private hovered: SimNode | null = null;
  private selected: string | null = null;
  private dragging: SimNode | null = null;
  private dragDx = 0;
  private dragDy = 0;
  private panning = false;
  private panFrom = { x: 0, y: 0, cx: 0, cy: 0 };
  private moved = false;

  private hidden = new Set<string>();
  private searchFocus: Set<string> | null = null;
  private neighbors = new Map<string, Set<string>>();
  private colorByNode = new Map<string, string>();
  private labelByNode = new Map<string, string>();
  private opts: EngineOptions = { showPulses: true, showLabels: true };
  private wasSettled = false;

  private ro: ResizeObserver;
  private canvas: HTMLCanvasElement;
  private cb: EngineCallbacks;

  constructor(canvas: HTMLCanvasElement, cb: EngineCallbacks) {
    this.canvas = canvas;
    this.cb = cb;
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) throw new Error('canvas 2d indisponível');
    this.ctx = ctx;

    // `passive: false` é obrigatório: o React registra onWheel como passivo, e
    // ali o preventDefault não segura o scroll da página.
    canvas.addEventListener('wheel', this.onWheel, { passive: false });
    canvas.addEventListener('pointerdown', this.onPointerDown);
    canvas.addEventListener('pointermove', this.onPointerMove);
    canvas.addEventListener('pointerup', this.onPointerUp);
    canvas.addEventListener('pointerleave', this.onPointerLeave);
    canvas.addEventListener('pointercancel', this.onPointerUp);

    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(canvas.parentElement ?? canvas);
    this.resize();

    this.raf = requestAnimationFrame(this.frame);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    const c = this.canvas;
    c.removeEventListener('wheel', this.onWheel);
    c.removeEventListener('pointerdown', this.onPointerDown);
    c.removeEventListener('pointermove', this.onPointerMove);
    c.removeEventListener('pointerup', this.onPointerUp);
    c.removeEventListener('pointerleave', this.onPointerLeave);
    c.removeEventListener('pointercancel', this.onPointerUp);
  }

  // ── Entrada de dados ─────────────────────────────────────────────────────

  setData(
    entities: EngineEntity[],
    relations: EngineRelation[],
    famOf: (type: string) => number,
    famTotal: number,
    colorOf: (type: string) => string,
  ) {
    this.sim.build(entities, relations, famOf, famTotal);

    this.neighbors = new Map();
    for (const r of relations) {
      if (!this.neighbors.has(r.source_id)) this.neighbors.set(r.source_id, new Set());
      if (!this.neighbors.has(r.target_id)) this.neighbors.set(r.target_id, new Set());
      this.neighbors.get(r.source_id)!.add(r.target_id);
      this.neighbors.get(r.target_id)!.add(r.source_id);
    }

    this.colorByNode = new Map(entities.map((e) => [e.id, colorOf(e.type)]));
    this.labelByNode = new Map(entities.map((e) => [e.id, e.name]));

    this.selected = null;
    this.hovered = null;
    this.autoFit = true;
    this.wasSettled = false;
    this.cb.onSettle(false);
  }

  setHidden(hidden: Set<string>) {
    this.hidden = hidden;
  }

  setSearchFocus(ids: Set<string> | null) {
    this.searchFocus = ids && ids.size ? ids : null;
  }

  setOptions(opts: Partial<EngineOptions>) {
    this.opts = { ...this.opts, ...opts };
  }

  select(id: string | null) {
    this.selected = id;
    this.cb.onSelect(id);
  }

  // ── Câmera ───────────────────────────────────────────────────────────────

  zoomBy(k: number) {
    this.autoFit = false;
    this.zoomAt(this.w / 2, this.h / 2, k);
  }

  fit(animated = true) {
    this.autoFit = true;
    if (!animated) this.applyFit(1);
  }

  /** Centraliza num nó sem mudar o zoom — usado pela busca e pela lista. */
  centerOn(id: string) {
    const n = this.sim.node(id);
    if (!n) return;
    this.autoFit = false;
    this.cam.x = this.w / 2 - n.x * this.cam.scale;
    this.cam.y = this.h / 2 - n.y * this.cam.scale;
  }

  private zoomAt(sx: number, sy: number, k: number) {
    const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, this.cam.scale * k));
    const ratio = next / this.cam.scale;
    // Zoom ancorado no ponteiro. O painel antigo escalava em torno da origem do
    // SVG, o que jogava o grafo pra fora da tela a cada scroll.
    this.cam.x = sx - (sx - this.cam.x) * ratio;
    this.cam.y = sy - (sy - this.cam.y) * ratio;
    this.cam.scale = next;
  }

  private applyFit(lerp: number) {
    const b = this.sim.bounds();
    const bw = Math.max(1, b.maxX - b.minX);
    const bh = Math.max(1, b.maxY - b.minY);
    const pad = 56;
    const target = Math.min(
      MAX_SCALE,
      Math.max(MIN_SCALE, Math.min((this.w - pad * 2) / bw, (this.h - pad * 2) / bh)),
    );
    const tx = this.w / 2 - ((b.minX + b.maxX) / 2) * target;
    const ty = this.h / 2 - ((b.minY + b.maxY) / 2) * target;
    this.cam.scale += (target - this.cam.scale) * lerp;
    this.cam.x += (tx - this.cam.x) * lerp;
    this.cam.y += (ty - this.cam.y) * lerp;
  }

  private resize() {
    const box = this.canvas.parentElement?.getBoundingClientRect();
    const w = Math.max(1, Math.floor(box?.width ?? this.canvas.clientWidth));
    const h = Math.max(1, Math.floor(box?.height ?? this.canvas.clientHeight));
    this.dpr = Math.min(2, window.devicePixelRatio || 1);
    this.w = w;
    this.h = h;
    this.canvas.width = Math.floor(w * this.dpr);
    this.canvas.height = Math.floor(h * this.dpr);
    this.canvas.style.width = `${w}px`;
    this.canvas.style.height = `${h}px`;
  }

  // ── Ponteiro ─────────────────────────────────────────────────────────────

  private toWorld(sx: number, sy: number) {
    return { x: (sx - this.cam.x) / this.cam.scale, y: (sy - this.cam.y) / this.cam.scale };
  }

  private local(e: PointerEvent | WheelEvent) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  private pick(sx: number, sy: number): SimNode | null {
    const p = this.toWorld(sx, sy);
    let best: SimNode | null = null;
    let bestD = Infinity;
    for (const n of this.sim.nodes) {
      if (this.hidden.has(n.id)) continue;
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      // Alvo maior que a marca: a 10% de zoom um soma tem 2 px e seria
      // impossível de acertar.
      const reach = Math.max(n.r + 6, 14 / this.cam.scale);
      if (d < reach && d < bestD) {
        bestD = d;
        best = n;
      }
    }
    return best;
  }

  private onWheel = (e: WheelEvent) => {
    e.preventDefault();
    const { x, y } = this.local(e);
    this.autoFit = false;
    this.zoomAt(x, y, e.deltaY < 0 ? 1.12 : 1 / 1.12);
  };

  private onPointerDown = (e: PointerEvent) => {
    const { x, y } = this.local(e);
    this.canvas.setPointerCapture(e.pointerId);
    this.moved = false;
    const hit = this.pick(x, y);
    if (hit) {
      const w = this.toWorld(x, y);
      this.dragging = hit;
      hit.fixed = true;
      this.dragDx = w.x - hit.x;
      this.dragDy = w.y - hit.y;
      this.autoFit = false;
    } else {
      this.panning = true;
      this.panFrom = { x: e.clientX, y: e.clientY, cx: this.cam.x, cy: this.cam.y };
      this.autoFit = false;
    }
  };

  private onPointerMove = (e: PointerEvent) => {
    const { x, y } = this.local(e);
    this.pointer = { x, y };
    if (Math.abs(e.movementX) + Math.abs(e.movementY) > 0) this.moved = true;

    if (this.dragging) {
      const w = this.toWorld(x, y);
      this.dragging.x = w.x - this.dragDx;
      this.dragging.y = w.y - this.dragDy;
      this.sim.reheat(0.3);
      return;
    }
    if (this.panning) {
      this.cam.x = this.panFrom.cx + (e.clientX - this.panFrom.x);
      this.cam.y = this.panFrom.cy + (e.clientY - this.panFrom.y);
    }
  };

  private onPointerUp = (e: PointerEvent) => {
    if (this.canvas.hasPointerCapture(e.pointerId)) this.canvas.releasePointerCapture(e.pointerId);
    const wasDragging = this.dragging;
    if (wasDragging) {
      wasDragging.fixed = false;
      // Só reaquece se o nó foi mesmo movido. Um clique seco reaquecia a
      // simulação e a rede inteira tremia (e o aviso "acomodando" voltava)
      // só por abrir um cartão de detalhe.
      if (this.moved) this.sim.reheat(0.25);
    }
    this.dragging = null;
    this.panning = false;

    // Clique = apertar e soltar sem arrastar. Arrastar um nó não deve abrir o
    // cartão de detalhe.
    if (!this.moved) {
      const { x, y } = this.local(e);
      const hit = this.pick(x, y);
      this.select(hit ? hit.id : null);
    }
  };

  private onPointerLeave = () => {
    this.pointer = null;
    if (this.hovered) {
      this.hovered = null;
      this.cb.onHover(null);
    }
  };

  // ── Quadro ───────────────────────────────────────────────────────────────

  private frame = () => {
    this.raf = requestAnimationFrame(this.frame);
    const time = (performance.now() - this.t0) / 1000;

    this.sim.step();
    if (this.autoFit) this.applyFit(this.sim.settled ? 0.12 : 0.06);

    if (this.sim.settled !== this.wasSettled) {
      this.wasSettled = this.sim.settled;
      this.cb.onSettle(this.sim.settled);
    }

    // Hover recalculado por quadro (e não só no mousemove) pra continuar certo
    // enquanto a rede se acomoda debaixo do cursor parado.
    if (this.pointer && !this.panning && !this.dragging) {
      const hit = this.pick(this.pointer.x, this.pointer.y);
      if (hit?.id !== this.hovered?.id) {
        this.hovered = hit;
        this.cb.onHover(hit ? hit.id : null);
      }
    }

    const anchor = this.hovered?.id ?? this.selected ?? null;
    let focus: Set<string> | null = null;
    if (anchor) {
      focus = new Set<string>([anchor]);
      for (const id of this.neighbors.get(anchor) ?? []) focus.add(id);
    } else if (this.searchFocus) {
      focus = this.searchFocus;
    }

    this.canvas.style.cursor = this.panning
      ? 'grabbing'
      : this.hovered
        ? 'pointer'
        : 'grab';

    this.renderer.draw(
      this.ctx,
      this.w,
      this.h,
      this.sim.nodes,
      this.sim.edges,
      {
        time,
        camera: this.cam,
        dpr: this.dpr,
        hovered: this.hovered,
        selected: this.selected,
        focus,
        hidden: this.hidden,
        colorOf: (n) => this.colorByNode.get(n.id) ?? '#8b8b9e',
        labelOf: (n) => this.labelByNode.get(n.id) ?? '',
        showPulses: this.opts.showPulses,
        showLabels: this.opts.showLabels,
      },
    );
  };
}
