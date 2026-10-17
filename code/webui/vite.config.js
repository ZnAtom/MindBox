import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  optimizeDeps: { include: ['three/addons/loaders/GLTFLoader.js'] },
  build: {
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        models: fileURLToPath(new URL('./models.html', import.meta.url)),
      },
    },
  },
});
