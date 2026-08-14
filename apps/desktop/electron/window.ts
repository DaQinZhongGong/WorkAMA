import { app, BrowserWindow } from 'electron';
import path from 'node:path';

// 窗口管理：主窗口、设置窗口、关于窗口统一在此创建并登记，便于 IPC 与菜单复用。
//
// 开发态（!app.isPackaged）从 http://localhost:20204（apps/web 开发服务器）加载 React 应用；
// 生产态加载 electron-vite 打包后的 out/renderer/index.html。
// 所有窗口均开启 contextIsolation + 关闭 nodeIntegration + sandbox，渲染进程仅通过 preload 暴露的
// window.workama API 与主进程通信（安全模型见《410》§5.3）。

const isDev = !app.isPackaged;
const DEV_URL = process.env.WORKAMA_DEV_URL ?? 'http://localhost:20204';

const windows = new Map<string, BrowserWindow>();

function preloadPath(): string {
  return path.join(app.getAppPath(), 'out', 'preload', 'index.js');
}

function rendererFile(): string {
  return path.join(app.getAppPath(), 'out', 'renderer', 'index.html');
}

function secureWebPreferences(): Electron.WebPreferences {
  return {
    preload: preloadPath(),
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
  };
}

export async function createMainWindow(): Promise<BrowserWindow> {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 1024,
    minHeight: 600,
    show: false,
    backgroundColor: '#0b0f1a',
    title: 'WorkAMA',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: secureWebPreferences(),
  });

  win.once('ready-to-show', () => win.show());

  if (isDev) {
    await win.loadURL(DEV_URL);
    win.webContents.openDevTools({ mode: 'detach' });
  } else {
    await win.loadFile(rendererFile());
  }

  win.on('closed', () => windows.delete('main'));
  windows.set('main', win);
  return win;
}

export function createSettingsWindow(parent?: BrowserWindow): BrowserWindow {
  const existing = windows.get('settings');
  if (existing && !existing.isDestroyed()) {
    existing.focus();
    return existing;
  }
  const win = new BrowserWindow({
    width: 720,
    height: 560,
    minWidth: 480,
    minHeight: 400,
    show: true,
    parent: parent ?? windows.get('main') ?? undefined,
    title: '设置 · WorkAMA',
    webPreferences: secureWebPreferences(),
  });
  win.on('closed', () => windows.delete('settings'));
  windows.set('settings', win);
  if (isDev) {
    void win.loadURL(`${DEV_URL}/settings`);
  } else {
    void win.loadFile(rendererFile());
  }
  return win;
}

export function createAboutWindow(parent?: BrowserWindow): BrowserWindow {
  const existing = windows.get('about');
  if (existing && !existing.isDestroyed()) {
    existing.focus();
    return existing;
  }
  const win = new BrowserWindow({
    width: 420,
    height: 360,
    resizable: false,
    minimizable: false,
    maximizable: false,
    show: true,
    parent: parent ?? windows.get('main') ?? undefined,
    title: '关于 · WorkAMA',
    webPreferences: secureWebPreferences(),
  });
  win.on('closed', () => windows.delete('about'));
  windows.set('about', win);
  if (isDev) {
    void win.loadURL(`${DEV_URL}/about`);
  } else {
    void win.loadFile(rendererFile());
  }
  return win;
}

export function getWindowById(id: string): BrowserWindow | undefined {
  return windows.get(id);
}

export function getAllDesktopWindows(): BrowserWindow[] {
  return Array.from(windows.values());
}

export function closeWindowById(id: string): boolean {
  const win = windows.get(id);
  if (win && !win.isDestroyed()) {
    win.close();
    return true;
  }
  return false;
}
