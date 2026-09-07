export type AppTheme = 'default' | 'glassmorphism' | 'glassdark' | 'minimal' | 'claymorphism';

export const APP_THEMES: AppTheme[] = ['default', 'glassmorphism', 'glassdark', 'minimal', 'claymorphism'];

export const THEME_LABELS: Record<AppTheme, { title: string; description: string }> = {
  default: {
    title: 'Default',
    description: 'Aparencia atual do Neo, opaca e estavel.',
  },
  glassmorphism: {
    title: 'Glassmorphism',
    description: 'Transparencia, superficies em vidro e efeitos nativos quando disponiveis.',
  },
  glassdark: {
    title: 'Cyberpunk',
    description: 'HUD cyberpunk 2077: vidro quase-preto, grid e scanline, cantos retos e vermelho neon. Base do Claude Design.',
  },
  minimal: {
    title: 'Minimalista',
    description: 'Visual sutil e neutro em tons de cinza, no estilo do app do ChatGPT no macOS.',
  },
  claymorphism: {
    title: 'Claymorphism',
    description: 'Superficies fofas tipo massinha, cantos arredondados e acento coral. Mais alegre.',
  },
};
