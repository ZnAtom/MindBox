import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const proxy = {
  '/api/inventory': {
    target: process.env.MIND_BOX_BACKEND_URL || 'http://127.0.0.1:8787',
    changeOrigin: true,
  },
  '/api/recording': {
    target: process.env.MIND_BOX_BACKEND_URL || 'http://127.0.0.1:8787',
    changeOrigin: true,
  },
  '/api/doors': {
    target: process.env.MIND_BOX_BRIDGE_URL || 'http://127.0.0.1:8765',
    changeOrigin: true,
  },
  '/api/camera': {
    target: process.env.MIND_BOX_BRIDGE_URL || 'http://127.0.0.1:8765',
    changeOrigin: true,
  },
  '/api/audio': {
    target: process.env.MIND_BOX_BRIDGE_URL || 'http://127.0.0.1:8765',
    changeOrigin: true,
  },
  '/api/vision': {
    target: process.env.MIND_BOX_BRIDGE_URL || 'http://127.0.0.1:8765',
    changeOrigin: true,
  },
  '/api/voice': {
    target: process.env.MIND_BOX_VOICE_URL || process.env.MIND_BOX_BACKEND_URL || 'http://127.0.0.1:8787',
    changeOrigin: true,
  },
  '/api/reminders': {
    target: process.env.MIND_BOX_BACKEND_URL || 'http://127.0.0.1:8787',
    changeOrigin: true,
  },
};

export default defineConfig({
  server: { proxy },
  preview: { proxy },
  optimizeDeps: { include: ['three/addons/loaders/GLTFLoader.js'] },
  build: {
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        models: fileURLToPath(new URL('./models.html', import.meta.url)),
        vision: fileURLToPath(new URL('./vision.html', import.meta.url)),
      },
    },
  },
});
