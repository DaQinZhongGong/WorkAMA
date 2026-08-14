import { BrowserWindow } from 'electron';
import { autoUpdater } from 'electron-updater';
import type { UpdateCheckResult, UpdateInfo } from 'electron-updater';

// 自动更新：基于 electron-updater，配合分渠道发布（stable/beta，设计文档《320》§4.2）。
// 默认不自动下载，仅在“有更新”时通过 IPC 通知渲染进程，由用户在 UI 上确认后再下载安装。

export function initAutoUpdater(mainWindow?: BrowserWindow): void {
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', (info: UpdateInfo) => {
    mainWindow?.webContents.send('updater:update-available', info);
  });
  autoUpdater.on('update-downloaded', (info: UpdateInfo) => {
    mainWindow?.webContents.send('updater:update-downloaded', info);
  });
  autoUpdater.on('error', (err: Error) => {
    mainWindow?.webContents.send('updater:error', err?.message ?? String(err));
  });

  // 启动后延迟检查更新，避免与启动流程抢资源
  setTimeout(() => {
    void checkForUpdates();
  }, 10_000);
}

export async function checkForUpdates(): Promise<boolean> {
  try {
    const result: UpdateCheckResult | null = await autoUpdater.checkForUpdates();
    return Boolean(result?.updateInfo);
  } catch {
    return false;
  }
}

export async function downloadAndInstallUpdate(): Promise<boolean> {
  try {
    await autoUpdater.downloadUpdate();
    await autoUpdater.quitAndInstall();
    return true;
  } catch {
    return false;
  }
}
