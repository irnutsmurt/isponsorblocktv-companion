import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../backend/companion/static', emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 600 },
  server: { proxy: { '/api': { target: 'http://localhost:8099', changeOrigin: false } } },
})
