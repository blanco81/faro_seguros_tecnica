import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// En desarrollo, Vite proxifica /api al backend local (sin CORS).
// En producción, lo hace nginx (ver frontend/nginx.conf).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
