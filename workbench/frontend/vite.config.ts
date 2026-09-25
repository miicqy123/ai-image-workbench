import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址通过 VITE_API_BASE 注入；开发默认 5173 代理到 8000
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/files': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/events': { target: 'http://127.0.0.1:8000', changeOrigin: true }
    }
  },
  build: { outDir: 'dist' }
})
