import { BrowserWindow, Menu, Tray, nativeImage } from 'electron';

// 系统托盘：最小化到托盘 + 右键菜单（显示/隐藏主窗口、退出）。
// 图标当前使用 nativeImage.createEmpty() 占位，正式发布前应替换为 build/icon.png 转出的 16x16/32x32 多尺寸 PNG。
// 与 Tauri 壳的 tray.rs 行为对齐：单击切换窗口可见性（设计文档《320》§4.2）。

let tray: Tray | null = null;

export function createTray(mainWindow: BrowserWindow): Tray {
  const icon = nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip('WorkAMA');

  const contextMenu = Menu.buildFromTemplate([
    { label: '显示主窗口', click: () => mainWindow.show() },
    { label: '隐藏主窗口', click: () => mainWindow.hide() },
    { type: 'separator' },
    {
      label: '最小化到托盘',
      type: 'checkbox',
      checked: true,
      click: (item) => {
        // 切换最小化到托盘行为（占位实现，保留后续偏好持久化扩展点）
        item.checked = !item.checked;
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        mainWindow.destroy();
      },
    },
  ]);
  tray.setContextMenu(contextMenu);

  tray.on('click', () => {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  return tray;
}

export function getTray(): Tray | null {
  return tray;
}

export function destroyTray(): void {
  tray?.destroy();
  tray = null;
}
