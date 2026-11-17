/**
 * Vocabulário de movimento do Modo Live — a parte que precisa ser IGUAL nas três
 * superfícies: o HUD que o webview desenha na janela (`LiveOverlayCyberpunk`), a
 * ilha da notch (asas com mic + stop) e o chip nativo logo abaixo dela.
 *
 * Antes cada uma tinha a própria curva de VU, então a MESMA voz movia as barras
 * de cada superfície numa altura diferente — o que se lia como "são dois apps".
 * Os tempos de animação continuam morando no CSS (`LiveOverlayCyberpunk.css`);
 * o `LiveMotion` do NeoNotchHost.swift copia os períodos de lá.
 */

/** Piso de ruído: abaixo disso é sala, não voz. */
const NOISE_FLOOR = 0.18;

/**
 * RMS do microfone → nível 0…1 para desenhar.
 *
 * Ganho 8 e gama 0.65 porque fala normal tem RMS ~0.02–0.15: sem ganho a barra
 * não sai do lugar, e sem o piso o ruído da sala deixa o equalizador inflado
 * parado. **Espelho exato de `LiveMotion.level(rms:)` no NeoNotchHost.swift** —
 * mexeu aqui, mexe lá.
 */
export function levelFromRms(rms: number): number {
  const curved = Math.pow(Math.min(1, Math.max(0, rms) * 8), 0.65);
  return Math.min(1, Math.max(0, (curved - NOISE_FLOOR) / (1 - NOISE_FLOOR)));
}
