import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the backend so the browser talks to one origin,
// exactly as it does in production behind nginx. Without that, cookie-based auth and
// the gateway return leg would behave differently in development than in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // The charting library is the largest dependency by some distance and is only
        // needed on the dashboard, comparison and report pages. Splitting it keeps the
        // sign-in page and the first paint small.
        manualChunks: {
          charts: ['recharts'],
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
