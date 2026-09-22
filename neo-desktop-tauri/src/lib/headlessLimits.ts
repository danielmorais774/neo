// Tetos do navegador headless local — persistidos no navegador do app e
// empurrados para o Rust no boot.
//
// O Rust é a fonte da verdade EM EXECUÇÃO (services/headless.rs), mas ele não
// persiste: um recreate do app voltaria aos padrões e os tetos que o usuário
// escolheu sumiriam sem aviso. Aqui é o mesmo padrão dos outros ajustes do
// desktop (mic, wake word): localStorage no front, empurrado no boot.

import type { HeadlessLimits } from './tauriApi';

const KEY = 'neo.headless.limits';

export type SavedLimits = {
  maxTabs: number;
  maxRamMb: number;
  idleTtlS: number;
  profile: 'dedicado' | 'logado';
  browserPath: string | null;
};

export function readSaved(): Partial<SavedLimits> | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Partial<SavedLimits>) : null;
  } catch {
    // Modo privado / storage bloqueado: o padrão do Rust vale, e é seguro.
    return null;
  }
}

export function save(limits: HeadlessLimits): void {
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        maxTabs: limits.max_tabs,
        maxRamMb: limits.max_ram_mb,
        idleTtlS: limits.idle_ttl_s,
        profile: limits.profile,
        browserPath: limits.browser_path,
      } satisfies SavedLimits),
    );
  } catch {
    /* sem storage: o valor vale só nesta execução */
  }
}

/** Empurra o que estiver salvo para o Rust. Chamado uma vez, no boot. */
export async function applySaved(): Promise<void> {
  const saved = readSaved();
  if (!saved) return;
  try {
    await window.electronAPI.headlessSetLimits(saved as never);
  } catch (e) {
    console.warn('[Neo] não consegui aplicar os tetos do headless:', e);
  }
}
