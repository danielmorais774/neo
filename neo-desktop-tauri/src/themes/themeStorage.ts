import type { AppTheme } from './themeTypes';
import { APP_THEMES } from './themeTypes';

const THEME_STORAGE_KEY = 'neo.theme';

export function isAppTheme(value: unknown): value is AppTheme {
  return typeof value === 'string' && APP_THEMES.includes(value as AppTheme);
}

export function readStoredTheme(): AppTheme {
  if (typeof window === 'undefined') return 'default';

  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return isAppTheme(stored) ? stored : 'default';
}

export function storeTheme(theme: AppTheme): void {
  window.localStorage.setItem(THEME_STORAGE_KEY, theme);
}
