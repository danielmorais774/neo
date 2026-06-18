import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // catálogo de vozes vem direto do neo-voice (Mac), não do neo-core (VPS)
      '/neo-voice': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/neo-voice/, ''),
      },
    },
  },
})
