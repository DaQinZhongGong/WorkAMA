import { app, BrowserWindow, Menu, nativeTheme, shell } from 'electron';
import { createAboutWindow, createSettingsWindow } from './window';

// 应用菜单：文件/编辑/视图/窗口/帮助，与 macOS 与 Windows/Linux 通用菜单约定保持一致。
// 视图菜单内提供主题切换（浅色/深色/跟随系统），帮助菜单提供关于、官网、检查更新入口。

export type ThemeMode = 'dark' | 'light' | 'system';

function applyTheme(theme: ThemeMode): void {
  nativeTheme.themeSource = theme;
}

export function buildMenu(mainWindow?: BrowserWindow): Menu {
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: '文件',
      submenu: [
        { label: '新建对话', accelerator: 'CmdOrCtrl+N', click: () => mainWindow?.show() },
        {
          label: '设置…',
          accelerator: 'CmdOrCtrl+,',
          click: () => createSettingsWindow(mainWindow),
        },
        { type: 'separator' },
        { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '重置缩放' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
        {
          label: '切换主题',
          submenu: [
            { label: '浅色', click: () => applyTheme('light') },
            { label: '深色', click: () => applyTheme('dark') },
            { label: '跟随系统', click: () => applyTheme('system') },
          ],
        },
      ],
    },
    {
      label: '窗口',
      submenu: [{ role: 'minimize', label: '最小化' }, { role: 'close', label: '关闭' }],
    },
    {
      label: '帮助',
      submenu: [
        { label: '关于 WorkAMA', click: () => createAboutWindow(mainWindow) },
        { label: '打开官网', click: () => void shell.openExternal('https://workama.ai') },
        {
          label: '检查更新…',
          click: () => mainWindow?.webContents.send('menu:check-updates'),
        },
      ],
    },
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
  return menu;
}
