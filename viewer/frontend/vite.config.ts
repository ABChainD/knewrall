import { defineConfig } from 'vite';

const apiPort = process.env['KNEWRALL_VIEWER_PORT'] ?? '8798';

export default defineConfig({
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${apiPort}`,
    },
  },
  build: {
    outDir: '../backend/static',
    emptyOutDir: true,
  },
});
