import { Effect, EffectState, getCurrentWindow } from '@tauri-apps/api/window';
import type { AppTheme } from './themeTypes';

const GLASS_EFFECT_CANDIDATES = [
  [Effect.Mica],
  [Effect.Acrylic],
  [Effect.WindowBackground],
  [Effect.UnderWindowBackground],
];

// Themes that run over a transparent native window with blurred desktop behind.
const GLASS_THEMES: AppTheme[] = ['glassmorphism', 'glassdark'];

function isGlassTheme(theme: AppTheme): boolean {
  return GLASS_THEMES.includes(theme);
}

// Native under-window tint per glass theme (RGBA 0..255). Glass Dark leans much
// darker and cooler so the desktop reads as a near-black ground behind the
// colored orbs, matching the Claude design.
const GLASS_TINTS: Partial<Record<AppTheme, [number, number, number, number]>> = {
  glassmorphism: [10, 14, 26, 96],
  glassdark: [10, 6, 8, 165], // near-black with a faint red lean (cyberpunk HUD)
};

function setDocumentTheme(theme: AppTheme): void {
  document.documentElement.dataset.theme = theme;
  const transparent = isGlassTheme(theme) ? 'transparent' : '';
  document.documentElement.style.background = transparent;
  document.body.style.background = transparent;
}

async function applyNativeGlassEffects(theme: AppTheme): Promise<void> {
  const currentWindow = getCurrentWindow();

  try {
    await currentWindow.setBackgroundColor([0, 0, 0, 0]);
  } catch {
    // Some platforms ignore or reject transparent native backgrounds.
  }

  const color = GLASS_TINTS[theme] ?? [10, 14, 26, 96];
  for (const effects of GLASS_EFFECT_CANDIDATES) {
    try {
      await currentWindow.setEffects({
        effects,
        state: EffectState.Active,
        radius: 0,
        color,
      });
      return;
    } catch {
      // Try the next platform-specific native effect.
    }
  }
}

// Opaque native window background per theme. Only visible in the rounded-corner
// gaps and during a live resize, but a matching color kills the color flash.
const OPAQUE_BACKGROUNDS: Partial<Record<AppTheme, [number, number, number, number]>> = {
  minimal: [33, 33, 33, 255], // #212121 — neutral gray, matches --app-bg
  claymorphism: [42, 36, 64, 255], // #2a2440 — soft violet clay ground, matches --app-bg
};

async function clearNativeGlassEffects(theme: AppTheme): Promise<void> {
  const currentWindow = getCurrentWindow();

  try {
    await currentWindow.clearEffects();
  } catch {
    // Native effects are best effort; CSS keeps the theme usable.
  }

  try {
    await currentWindow.setBackgroundColor(OPAQUE_BACKGROUNDS[theme] ?? [10, 14, 26, 255]);
  } catch {
    // Background color changes are not supported everywhere.
  }
}

export async function applyThemeRuntime(theme: AppTheme): Promise<void> {
  setDocumentTheme(theme);

  if (isGlassTheme(theme)) {
    await applyNativeGlassEffects(theme);
  } else {
    await clearNativeGlassEffects(theme);
  }
}
