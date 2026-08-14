import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': { target: 'http://localhost:20200', changeOrigin: true },
      '/ws': { target: 'ws://localhost:20201', ws: true, changeOrigin: true },
    },
  },
  preview: { host: '0.0.0.0', port: 3000, allowedHosts: true },
  test: { environment: 'jsdom', include: ['src/**/*.spec.ts', 'src/**/*.spec.tsx', 'src/**/*.test.ts', 'src/**/*.test.tsx'] },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'vendor-react'
            if (id.includes('react-router')) return 'vendor-react'
            if (id.includes('@tanstack')) return 'vendor-query'
            if (id.includes('zustand')) return 'vendor-query'
            if (id.includes('lucide-react')) return 'vendor-icons'
            return
          }
          if (/[\\/]packages[\\/](i18n|event-renderer|api-client|config)[\\/]/.test(id)) return 'vendor-workama'
        },
      },
    },
  },
})
