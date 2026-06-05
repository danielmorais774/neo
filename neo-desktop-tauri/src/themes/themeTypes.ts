export type AppTheme = 'default' | 'glassmorphism';

export const APP_THEMES: AppTheme[] = ['default', 'glassmorphism'];

export const THEME_LABELS: Record<AppTheme, { title: string; description: string }> = {
  default: {
    title: 'Default',
    description: 'Aparencia atual do Neo, opaca e estavel.',
  },
  glassmorphism: {
    title: 'Glassmorphism',
    description: 'Transparencia, superficies em vidro e efeitos nativos quando disponiveis.',
  },
};
