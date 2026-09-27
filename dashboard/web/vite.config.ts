import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// In dev the browser talks to Vite on :5173; everything that is not the SPA
// (login, callback, API proxy, health) is forwarded to the BFF on :3000, so the
// browser still sees one origin and the session cookie stays first-party.
const bff = process.env.BFF_URL ?? 'http://127.0.0.1:3000';

// Modes B/C (`vite build --mode smart`, npm run build:smart): the SMART build is
// served by OpenEMR from the Co-Pilot module's public folder. DASHBOARD_BASE
// changes the URL path (an OpenEMR webroot other than "/"); DASHBOARD_OUT_DIR the
// folder it is written to.
const MODULE_DASHBOARD = '/interface/modules/custom_modules/oe-module-copilot/public/dashboard/';
const moduleOutDir = fileURLToPath(new URL(`../..${MODULE_DASHBOARD}`, import.meta.url));

export default defineConfig(({ mode }) => {
  const smart = mode === 'smart';
  return {
    plugins: [react()],
    base: smart ? (process.env.DASHBOARD_BASE ?? MODULE_DASHBOARD) : '/',
    // smart-public/ holds the folder's .htaccess (CSP, framing, no listing).
    publicDir: smart ? 'smart-public' : 'public',
    define: smart ? { 'import.meta.env.VITE_TRANSPORT': JSON.stringify('smart') } : {},
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
      outDir: smart ? (process.env.DASHBOARD_OUT_DIR ?? moduleOutDir) : 'dist',
      // The module folder is outside this project; it only ever holds this build.
      emptyOutDir: true,
      sourcemap: false,
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./test/setup.ts'],
      include: ['test/**/*.test.{ts,tsx}'],
    },
  };
});
