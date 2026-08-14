import { defineConfig } from 'vitest/config';

// 仅在 apps/desktop 范围内运行测试，使用 Node 环境避免拉起真实浏览器/Electron。
// electron / electron-updater 在测试文件内通过 vi.mock 完全替换，不依赖真实二进制。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.spec.ts'],
  },
});
