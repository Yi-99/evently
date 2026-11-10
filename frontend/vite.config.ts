import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'
import { extractMiddleware } from './server/extract.ts'

// Serves POST /api/extract (Claude-backed intake extraction) from the dev and preview servers.
// Fallback only: the app calls the backend's POST /api/v1/intake/extract first.
// Middlewares run before the proxy, so /api/extract never reaches the backend.
const extractApi = (): Plugin => ({
  name: 'extract-api',
  configureServer: (server) => void server.middlewares.use(extractMiddleware),
  configurePreviewServer: (server) => void server.middlewares.use(extractMiddleware),
})

// FastAPI backend (task dev in backend/; uvicorn binds 127.0.0.1, so not "localhost", which may resolve to ::1).
// Override with BACKEND_URL when it runs elsewhere.
const backend = process.env.BACKEND_URL ?? 'http://127.0.0.1:8000'
const proxy = {
  '/api': { target: backend, changeOrigin: true },
  '/ws': { target: backend, changeOrigin: true, ws: true },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), extractApi()],
  server: { port: 5173, proxy },
  preview: { proxy },
})
