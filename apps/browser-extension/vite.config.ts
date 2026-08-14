/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        popup: resolve(root, 'popup.html'),
        sidebar: resolve(root, 'sidebar.html'),
        background: resolve(root, 'src/background.ts'),
        content: resolve(root, 'src/content.ts'),
      },
      output: {
        // background 与 content 需要保持稳定文件名，方便 manifest 引用
        entryFileNames: (chunk) =>
          chunk.name === 'background' || chunk.name === 'content'
            ? '[name].js'
            : 'assets/[name].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  test: {
    // 弹窗 UI 测试需要 DOM 环境
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./__tests__/setup.ts'],
    include: ['__tests__/**/*.test.{ts,tsx}'],
    coverage: {
      reporter: ['text', 'html'],
    },
  },
})
