import { app, BrowserWindow } from 'electron';
import { createMainWindow } from './window';
import { createTray } from './tray';
import { buildMenu } from './menu';
import { registerIpcHandlers } from './ipc-handlers';
import { initAutoUpdater } from './updater';

// AMA-Work 桌面客户端主进程入口。
//
// 装配顺序（参见设计文档《320》§4.2 桌面端）：
//   1. 注册 IPC 处理器（auth / api / notification / system / theme / window）
//   2. 创建主窗口（1280x800，最小 1024x600，加载 React 应用）
//   3. 装配应用菜单与系统托盘
//   4. 初始化 electron-updater 自动更新
//
// bootstrap 被导出便于单元测试在不真实启动 Electron 的前提下直接调用（依赖已被 vi.mock 替换）。

let mainWindow: BrowserWindow | null = null;

export async function bootstrap(): Promise<BrowserWindow> {
  registerIpcHandlers();
  mainWindow = await createMainWindow();
  buildMenu(mainWindow);
  createTray(mainWindow);
  initAutoUpdater(mainWindow);
  return mainWindow;
}

export function getMainWindow(): BrowserWindow | null {
  return mainWindow;
}

app.whenReady().then(() => {
  void bootstrap();
});

app.on('window-all-closed', () => {
  // macOS 上保留应用活跃，其余平台直接退出
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    void bootstrap();
  }
});
