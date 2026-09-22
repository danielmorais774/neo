/*
 * Cor do grafo.
 *
 * O painel antigo tinha um mapa de 12 tipos em português (refeicao, treino,
 * suplemento…) que o PKS não emite mais: hoje os tipos são exercise, document,
 * preference, device, person… Resultado medido em 2026-09-19 — nenhum dos 17
 * tipos no banco batia com o mapa, e os 103 nós saíam todos no mesmo cinza.
 *
 * Também não dá pra só traduzir o mapa: 17 tipos = 17 matizes, e num grafo
 * qualquer par de nós pode encostar (é uma forma "all-pairs"). Acima de ~8
 * matizes não existe ordenação que passe nos pisos de daltonismo. Então a cor
 * carrega a FAMÍLIA (5 matizes) e o tipo exato vai em texto — no rótulo do nó,
 * no chip da legenda e no cartão de detalhe. Identidade nunca fica só na cor.
 *
 * Os 5 matizes foram escolhidos por busca sobre o espaço OKLCH e conferidos no
 * validador da skill dataviz contra a superfície do painel (#08080d), modo
 * escuro, --pairs all:
 *
 *   faixa de luminosidade  pass   os 5 dentro de L 0.48–0.67
 *   piso de croma          pass   os 5 >= 0.1
 *   separação CVD          pass   pior par #e35413↔#0da26b ΔE 9.7 (deutan)   [alvo >= 8]
 *   piso de visão normal   pass   pior par #b42c92↔#724ed8 ΔE 17.0           [piso >= 15]
 *   contraste vs fundo     pass   os 5 >= 3:1
 *
 * Mexer num hex aqui exige rodar o validador de novo — os pares se movem todos
 * juntos.
 */

export interface Family {
  id: string;
  label: string;
  color: string;
  types: string[];
}

export const FAMILIES: Family[] = [
  { id: 'pessoas',  label: 'Pessoas e contas',  color: '#724ed8', types: ['person', 'company', 'identity', 'account', 'organization'] },
  { id: 'sistemas', label: 'Máquinas e código', color: '#1392d4', types: ['device', 'technology', 'software', 'service', 'tool'] },
  { id: 'corpo',    label: 'Corpo e treino',    color: '#0da26b', types: ['exercise', 'workout', 'muscle_group', 'meal', 'food', 'supplement'] },
  { id: 'saber',    label: 'Documentos e gostos', color: '#b42c92', types: ['document', 'preference', 'correction_example', 'fact', 'note'] },
  { id: 'rumo',     label: 'Projetos e lugares', color: '#e35413', types: ['project', 'goal', 'routine', 'place', 'event'] },
];

/** Tipo fora das famílias conhecidas cai aqui — a regra "dobra em Outros", e
 *  não "gera mais um matiz", que é o que estoura os pisos de daltonismo. */
export const OTHER: Family = { id: 'outros', label: 'Outros', color: '#8b8b9e', types: [] };

const BY_TYPE = new Map<string, Family>();
for (const fam of FAMILIES) for (const t of fam.types) BY_TYPE.set(t, fam);

export const familyOf = (type: string): Family => BY_TYPE.get(type) ?? OTHER;
export const colorOf = (type: string): string => familyOf(type).color;

/** Índice estável da família — o motor usa número, não string, no laço quente. */
export const familyIndex = (type: string): number => {
  const fam = BY_TYPE.get(type);
  return fam ? FAMILIES.indexOf(fam) : FAMILIES.length;
};

export const ALL_FAMILIES: Family[] = [...FAMILIES, OTHER];

/** `#rrggbb` → [r, g, b]. Memorizado porque o render pede isso por nó/quadro. */
const rgbCache = new Map<string, [number, number, number]>();
export function rgb(hex: string): [number, number, number] {
  let v = rgbCache.get(hex);
  if (!v) {
    v = [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16),
    ];
    rgbCache.set(hex, v);
  }
  return v;
}

export function rgba(hex: string, alpha: number): string {
  const [r, g, b] = rgb(hex);
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Mistura usada nas arestas: uma cor chapada no meio do caminho entre as duas
 *  pontas. Evita criar um createLinearGradient por aresta por quadro. */
export function mix(hexA: string, hexB: string, alpha: number): string {
  const [r1, g1, b1] = rgb(hexA);
  const [r2, g2, b2] = rgb(hexB);
  return `rgba(${(r1 + r2) >> 1},${(g1 + g2) >> 1},${(b1 + b2) >> 1},${alpha})`;
}
