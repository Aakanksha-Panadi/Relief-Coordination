import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend already sends CORS_ORIGINS=*, so the app can call it directly.
// The proxy is here so `/api/...` works too if you ever lock CORS down.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE || 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
