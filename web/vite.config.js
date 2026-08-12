import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development Vite serves the UI and proxies /api to the Python process, so
// the browser only ever talks to one origin and there is no CORS to reason
// about. `npm run build` emits into web/dist, which api.py serves directly.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8501',
        changeOrigin: true,
      },
    },
  },
})
