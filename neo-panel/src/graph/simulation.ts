/*
 * Layout dirigido a forças, versão viva.
 *
 * O painel antigo rodava 400 iterações O(N²) SÍNCRONAS dentro do fetch, antes
 * de pintar o primeiro pixel. Medido em 2026-09-19 com esse mesmo código:
 * 16 ms nos 103 nós de hoje, 42 ms em 250, 162 ms no teto de 500 do endpoint.
 * Ou seja: incômodo já hoje, e uma trava de verdade quando o PKS crescer.
 *
 * Aqui a simulação é um passo por quadro, dentro do mesmo requestAnimationFrame
 * do desenho: nada bloqueia em momento nenhum, e a rede se acomoda na tela —
 * que por acaso é o visual que a gente quer (um tecido se organizando). A
 * repulsão usa grade espacial e só olha vizinhos dentro do raio, então o custo
 * anda com a densidade local, não com N².
 */

export interface SimNode {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;        // raio do soma, vem do grau
  fam: number;      // índice de família (coesão + cor)
  degree: number;
  phase: number;    // fase do "respiro", fixa por nó
  fixed: boolean;   // sendo arrastado
}

export interface SimEdge {
  id: string;
  type: string;
  a: SimNode;
  b: SimNode;
  w: number;        // espessura, da confiança
  bow: number;      // curvatura do dendrito (sinal estável por aresta)
  offset: number;   // defasagem do pulso sináptico
  speed: number;
}

/* Os números abaixo foram ajustados contra o grafo real de 2026-09-19 (103 nós,
 * 91 relações). Com os valores iniciais a rede fechava numa bola de ~600 px:
 * os somas encostavam uns nos outros e os nomes viravam um borrão. O que abriu
 * foi repulsão com MASSA (hub empurra mais que folha), alcance maior que o
 * diâmetro típico do grafo, e folga de colisão maior que o rótulo. */
const REPULSION = 190;       // multiplicado pela massa dos dois nós
const REPULSE_RADIUS = 620;  // > diâmetro típico: clusters distantes ainda se afastam
const SPRING_BASE = 96;      // comprimento de repouso, ANTES de somar os raios
const SPRING_K = 0.045;
const COLLIDE_PAD = 26;
const COHESION = 0.003;      // puxão fraco pro centro da própria família
const GRAVITY = 0.005;       // só segura quem não tem aresta; centralizar é da câmera
const DAMPING = 0.82;
const ALPHA_DECAY = 0.977;
const ALPHA_MIN = 0.004;
const MAX_SPEED = 30;

/** Massa: um hub de grau 20 tem que abrir espaço pros vinte filhos. */
const mass = (n: SimNode) => 1 + Math.sqrt(n.degree) * 1.6;

/** Hash determinístico: mesma aresta, mesma curvatura e mesma fase em toda
 *  recarga. Sem isso os dendritos "piscam" de lado a cada refresh. */
function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

export class Simulation {
  nodes: SimNode[] = [];
  edges: SimEdge[] = [];
  alpha = 1;

  private byId = new Map<string, SimNode>();
  private cell = REPULSE_RADIUS;
  private grid = new Map<number, SimNode[]>();
  private famCount = 0;

  build(
    entities: { id: string; type: string }[],
    relations: { id: string; source_id: string; target_id: string; type: string; confidence: number }[],
    famOf: (type: string) => number,
    famTotal: number,
  ) {
    this.famCount = famTotal;
    const degree = new Map<string, number>();
    for (const r of relations) {
      degree.set(r.source_id, (degree.get(r.source_id) ?? 0) + 1);
      degree.set(r.target_id, (degree.get(r.target_id) ?? 0) + 1);
    }

    const prev = this.byId;
    this.byId = new Map();
    this.nodes = entities.map((e) => {
      const fam = famOf(e.type);
      const deg = degree.get(e.id) ?? 0;
      const old = prev.get(e.id);
      // Semeia por família num anel: já nasce agrupado, então a simulação gasta
      // menos quadros desembaraçando e o resultado é reprodutível.
      const famAngle = ((fam + 0.5) / Math.max(1, famTotal)) * Math.PI * 2;
      const jitter = hash(e.id);
      const node: SimNode = {
        id: e.id,
        x: old?.x ?? Math.cos(famAngle) * 260 + (jitter - 0.5) * 220,
        y: old?.y ?? Math.sin(famAngle) * 260 + (hash(e.id + '#y') - 0.5) * 220,
        vx: 0,
        vy: 0,
        r: 5.5 + Math.sqrt(deg) * 3.4,
        fam,
        degree: deg,
        phase: jitter * Math.PI * 2,
        fixed: false,
      };
      this.byId.set(e.id, node);
      return node;
    });

    this.edges = [];
    for (const r of relations) {
      const a = this.byId.get(r.source_id);
      const b = this.byId.get(r.target_id);
      if (!a || !b || a === b) continue;
      const h = hash(r.id);
      this.edges.push({
        id: r.id,
        type: r.type,
        a,
        b,
        w: 0.6 + Math.min(1, r.confidence) * 1.3,
        bow: (h < 0.5 ? -1 : 1) * (0.08 + h * 0.1),
        offset: h,
        speed: 0.16 + h * 0.16,
      });
    }

    this.alpha = 1;
  }

  node(id: string): SimNode | undefined {
    return this.byId.get(id);
  }

  /** Volta a esquentar: usado ao arrastar um nó e ao trocar de workspace. */
  reheat(to = 0.55) {
    this.alpha = Math.max(this.alpha, to);
  }

  get settled(): boolean {
    return this.alpha <= ALPHA_MIN;
  }

  step() {
    if (this.alpha <= ALPHA_MIN) return;
    const { nodes, edges } = this;
    const n = nodes.length;
    if (n === 0) return;

    // ── Repulsão por grade espacial ────────────────────────────────────────
    this.grid.clear();
    const key = (cx: number, cy: number) => cx * 73856093 ^ cy * 19349663;
    for (const p of nodes) {
      const k = key(Math.floor(p.x / this.cell), Math.floor(p.y / this.cell));
      const bucket = this.grid.get(k);
      if (bucket) bucket.push(p);
      else this.grid.set(k, [p]);
    }

    for (const a of nodes) {
      const cx = Math.floor(a.x / this.cell);
      const cy = Math.floor(a.y / this.cell);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const bucket = this.grid.get(key(cx + ox, cy + oy));
          if (!bucket) continue;
          for (const b of bucket) {
            if (b === a) continue;
            let dx = b.x - a.x;
            let dy = b.y - a.y;
            let d2 = dx * dx + dy * dy;
            if (d2 > REPULSE_RADIUS * REPULSE_RADIUS) continue;
            if (d2 < 1) {
              // Coincidentes: empurra por uma direção estável, não aleatória,
              // senão o par fica tremendo pra sempre.
              dx = (a.phase - b.phase) || 0.7;
              dy = 0.7;
              d2 = dx * dx + dy * dy;
            }
            const d = Math.sqrt(d2);
            // Piso na distância: sem ele dois nós quase colados geram uma força
            // gigante e o par sai disparado pra fora da tela.
            const f = (REPULSION * mass(a) * mass(b)) / Math.max(d2, 400);
            a.vx -= (dx / d) * f;
            a.vy -= (dy / d) * f;
          }
        }
      }
    }

    // ── Molas nas arestas ──────────────────────────────────────────────────
    for (const e of edges) {
      const dx = e.b.x - e.a.x;
      const dy = e.b.y - e.a.y;
      const d = Math.hypot(dx, dy) || 1;
      // O repouso soma os raios: um hub gordo precisa de braço mais comprido
      // pra que o filho não fique em cima do próprio soma.
      const rest = SPRING_BASE + e.a.r + e.b.r;
      const f = (d - rest) * SPRING_K;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      e.a.vx += fx;
      e.a.vy += fy;
      e.b.vx -= fx;
      e.b.vy -= fy;
    }

    // ── Coesão por família ─────────────────────────────────────────────────
    // É o que dá cara de córtex: mesma família fica no mesmo bairro, e os 23
    // nós sem nenhuma aresta param de vagar pro infinito (só a gravidade
    // central empilhava todos eles em cima do meio).
    const sx = new Float64Array(this.famCount + 1);
    const sy = new Float64Array(this.famCount + 1);
    const cnt = new Float64Array(this.famCount + 1);
    for (const p of nodes) {
      sx[p.fam] += p.x;
      sy[p.fam] += p.y;
      cnt[p.fam]++;
    }
    for (const p of nodes) {
      const c = cnt[p.fam];
      if (c > 1) {
        p.vx += (sx[p.fam] / c - p.x) * COHESION;
        p.vy += (sy[p.fam] / c - p.y) * COHESION;
      }
      p.vx += -p.x * GRAVITY;
      p.vy += -p.y * GRAVITY;
    }

    // ── Colisão dos somas ──────────────────────────────────────────────────
    for (const a of nodes) {
      const cx = Math.floor(a.x / this.cell);
      const cy = Math.floor(a.y / this.cell);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const bucket = this.grid.get(key(cx + ox, cy + oy));
          if (!bucket) continue;
          for (const b of bucket) {
            if (b === a) continue;
            const min = a.r + b.r + COLLIDE_PAD;
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const d = Math.hypot(dx, dy);
            if (d >= min || d === 0) continue;
            const push = ((min - d) / d) * 0.5;
            a.x -= dx * push;
            a.y -= dy * push;
            b.x += dx * push;
            b.y += dy * push;
          }
        }
      }
    }

    // ── Integração ─────────────────────────────────────────────────────────
    for (const p of nodes) {
      if (p.fixed) {
        p.vx = 0;
        p.vy = 0;
        continue;
      }
      p.vx *= DAMPING;
      p.vy *= DAMPING;
      const sp = Math.hypot(p.vx, p.vy);
      if (sp > MAX_SPEED) {
        p.vx = (p.vx / sp) * MAX_SPEED;
        p.vy = (p.vy / sp) * MAX_SPEED;
      }
      p.x += p.vx * this.alpha;
      p.y += p.vy * this.alpha;
    }

    this.alpha *= ALPHA_DECAY;
  }

  /** Caixa que contém tudo — usada pra enquadrar a rede ao abrir. */
  bounds() {
    if (!this.nodes.length) return { minX: -1, minY: -1, maxX: 1, maxY: 1 };
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const p of this.nodes) {
      if (p.x - p.r < minX) minX = p.x - p.r;
      if (p.y - p.r < minY) minY = p.y - p.r;
      if (p.x + p.r > maxX) maxX = p.x + p.r;
      if (p.y + p.r > maxY) maxY = p.y + p.r;
    }
    return { minX, minY, maxX, maxY };
  }
}
