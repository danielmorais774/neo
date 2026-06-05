import { Effect, EffectState, getCurrentWindow } from '@tauri-apps/api/window';
import type { AppTheme } from './themeTypes';

const GLASS_EFFECT_CANDIDATES = [
  [Effect.Mica],
  [Effect.Acrylic],
  [Effect.WindowBackground],
  [Effect.UnderWindowBackground],
];

function setDocumentTheme(theme: AppTheme): void {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.background = theme === 'glassmorphism' ? 'transparent' : '';
  document.body.style.background = theme === 'glassmorphism' ? 'transparent' : '';
}

async function applyNativeGlassEffects(): Promise<void> {
  const currentWindow = getCurrentWindow();

  try {
    await currentWindow.setBackgroundColor([0, 0, 0, 0]);
  } catch {
    // Some platforms ignore or reject transparent native backgrounds.
  }

  for (const effects of GLASS_EFFECT_CANDIDATES) {
    try {
      await currentWindow.setEffects({
        effects,
        state: EffectState.Active,
        radius: 0,
        color: [10, 14, 26, 96],
      });
      return;
    } catch {
      // Try the next platform-specific native effect.
    }
  }
}

async function clearNativeGlassEffects(): Promise<void> {
  const currentWindow = getCurrentWindow();

  try {
    await currentWindow.clearEffects();
  } catch {
    // Native effects are best effort; CSS keeps the theme usable.
  }

  try {
    await currentWindow.setBackgroundColor([10, 14, 26, 255]);
  } catch {
    // Background color changes are not supported everywhere.
  }
}

export async function applyThemeRuntime(theme: AppTheme): Promise<void> {
  setDocumentTheme(theme);

  if (theme === 'glassmorphism') {
    await applyNativeGlassEffects();
  } else {
    await clearNativeGlassEffects();
  }
}
