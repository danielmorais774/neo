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
      // O painel fala DIRETO com o neo-voice (o neo-core na VPS não o alcança).
      //
      // Tem de ser o MESMO neo-voice que o desktop usa — o do neo-host na LAN.
      // Antes isto apontava p/ 127.0.0.1:5002, que hoje é o Core Tunnel pro
      // neo-voice velho da VPS: o painel listava vozes e ligava/desligava o
      // fallback numa instância que NÃO é a que fala no Modo Live (e o toggle do
      // hedge nem existe lá — dava "neo-voice indisponível").
      '/neo-voice': {
        target: process.env.NEO_VOICE_URL || 'http://192.168.4.210:5002',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/neo-voice/, ''),
      },
    },
  },
})
