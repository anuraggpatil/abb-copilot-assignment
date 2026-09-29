/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Port 5173 is not incidental: it is the origin the backend allows by default
// (`BACKEND_CORS_ORIGINS` in apps/backend/config.py). Change one and change the other.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true },
  build: { outDir: 'dist', sourcemap: true },
  // Component tests run in jsdom against the real components; only `./api` is substituted, so
  // what they exercise is the conversation state machine and the markup rather than a
  // re-description of it.
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
})
