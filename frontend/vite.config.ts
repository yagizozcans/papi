import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import { tanstackRouter } from '@tanstack/router-plugin/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

const PAPI = process.env.PAPI_API ?? 'http://127.0.0.1:38417'

export default defineConfig({
  base: '/app/',
  plugins: [
    tanstackRouter({ target: 'react', autoCodeSplitting: true }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../src/papi/web/ui',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/v1': {
        target: PAPI,
        changeOrigin: true,
        configure: (proxy) => {
          // papi rejects non-GET requests whose Origin host differs from Host.
          proxy.on('proxyReq', (proxyReq) => {
            if (proxyReq.getHeader('origin')) proxyReq.setHeader('origin', PAPI)
          })
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: false,
    css: false,
  },
})
