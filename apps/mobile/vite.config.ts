import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { host: '0.0.0.0', port: 3100 },
  preview: { host: '0.0.0.0', port: 3100 },
  test: { environment: 'jsdom', globals: true, exclude: ['e2e/**', 'node_modules/**', 'dist/**'] },
})
