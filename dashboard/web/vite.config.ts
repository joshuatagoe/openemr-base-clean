import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// In dev the browser talks to Vite on :5173; everything that is not the SPA
// (login, callback, API proxy, health) is forwarded to the BFF on :3000, so the
// browser still sees one origin and the session cookie stays first-party.
const bff = process.env.BFF_URL ?? 'http://127.0.0.1:3000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/auth': bff,
      '/api': bff,
      '/healthz': bff,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./test/setup.ts'],
    include: ['test/**/*.test.{ts,tsx}'],
  },
});
