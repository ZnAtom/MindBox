import { defineConfig } from 'vite';

const proxy = {
  '/api/doors': {
    target: process.env.MIND_BOX_BRIDGE_URL || 'http://127.0.0.1:8765',
    changeOrigin: true,
  },
  '/api/camera': {
    target: process.env.MIND_BOX_BRIDGE_URL || 'http://127.0.0.1:8765',
    changeOrigin: true,
  },
};

export default defineConfig({
  server: { proxy },
  preview: { proxy },
});
