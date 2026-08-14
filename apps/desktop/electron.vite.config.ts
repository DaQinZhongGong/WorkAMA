import { defineConfig } from 'electron-vite';
import { resolve } from 'node:path';

// AMA-Work Electron 构建配置：
// - main：主进程入口 electron/main.ts → out/main/main.js（package.json 的 "main" 字段指向它）
// - preload：预加载脚本 electron/preload.ts → out/preload/index.js（window.ts 中 preloadPath 引用它）
// - renderer：渲染进程 src/index.html → out/renderer/index.html（生产态由主窗口 loadFile 加载）
//
// 开发态下 main.ts 检测 !app.isPackaged 后从 http://localhost:20204（apps/web 开发服务器）加载，
// 因此 `pnpm electron:dev` 前请先在 apps/web 启动 `pnpm dev`，或在 main.ts 中改用桌面自带 renderer。
export default defineConfig({
  main: {
    build: {
      outDir: 'out/main',
      rollupOptions: { input: { index: resolve(__dirname, 'electron/main.ts') } },
    },
  },
  preload: {
    build: {
      outDir: 'out/preload',
      rollupOptions: { input: { index: resolve(__dirname, 'electron/preload.ts') } },
    },
  },
  renderer: {
    root: 'src',
    build: {
      outDir: 'out/renderer',
      rollupOptions: { input: { index: resolve(__dirname, 'src/index.html') } },
    },
  },
});
