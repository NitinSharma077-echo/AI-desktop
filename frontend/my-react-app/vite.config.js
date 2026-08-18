import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In production FastAPI serves the built dist/ itself, so the app is always
// same-origin and every fetch can use a bare path like `/auth/token`.
//
// `npm run dev` breaks that assumption -- Vite serves on :5173 while the API is
// on :8000 -- so the dev server proxies the API paths across. Without this the
// same bare paths would hit Vite and return index.html, and every call would
// fail on "Unexpected token '<'" rather than anything that names the problem.
const API_PATHS = ['/auth', '/chat', '/documents', '/health', '/zoho', '/docs', '/openapi.json']

// 8001, not the usual 8000: another app on this machine already owns 8000, and
// a proxy pointing at the wrong server is a nasty failure -- requests succeed,
// they just reach something else. Override with API_ORIGIN if you move it.
const API_ORIGIN = process.env.API_ORIGIN || 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      API_PATHS.map((path) => [path, { target: API_ORIGIN, changeOrigin: true }]),
    ),
  },
})
