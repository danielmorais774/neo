import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { AppTheme } from './themeTypes';
import { ThemeContext } from './themeContext';
import type { ThemeContextValue } from './themeContext';
import { applyThemeRuntime } from './themeRuntime';
import { readStoredTheme, storeTheme } from './themeStorage';

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<AppTheme>(() => readStoredTheme());

  useEffect(() => {
    storeTheme(theme);
    applyThemeRuntime(theme).catch((error) => {
      console.warn('[Neo] Failed to apply native theme runtime:', error);
    });
  }, [theme]);

  const value = useMemo<ThemeContextValue>(
    () => ({
      theme,
      setTheme: setThemeState,
    }),
    [theme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
