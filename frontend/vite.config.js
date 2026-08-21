import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In production the reverse proxy (Caddy by default, Traefik with the
// override) routes /api to the backend container, so the frontend and the API
// share an origin and CORS never enters the picture. The dev proxy below
// reproduces that arrangement locally, so a path that works in `npm run dev`
// works in the container too.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
