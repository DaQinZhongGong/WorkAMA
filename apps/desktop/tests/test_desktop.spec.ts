// AMA-Work 桌面客户端测试套件。
//
// 设计说明（参见《320》§4.2 桌面端 / 《410》§5.3 安全模型）：
// - 不真实启动 Electron。所有 Electron API（app / BrowserWindow / Menu / Tray /
//   nativeImage / nativeTheme / Notification / shell / ipcMain / contextBridge /
//   ipcRenderer）以及 electron-updater.autoUpdater 均通过 vi.mock 替换。
// - 通过三种手段验证行为：
//   1. 检查 mock 调用参数 → 验证构造与配置（窗口选项、菜单模板、托盘等）
//   2. 直接调用收集到的 IPC handler/listener → 验证 IPC 行为（auth/api/theme/window）
//   3. 调用导出的纯函数 → 验证业务逻辑（login / apiCall / theme / tray 等）
// - global.fetch 在需要时通过 vi.stubGlobal 替换，覆盖登录与 API 调用路径。
// - main.ts 在模块加载时调用 app.whenReady().then(bootstrap)；测试中将
//   app.whenReady 设为返回未决 Promise，避免 bootstrap 在 import 阶段自动运行，
//   改由测试显式调用 bootstrap() 以验证装配流程。

import { beforeEach, describe, expect, it, vi } from 'vitest';

// ---------------------------------------------------------------------------
// 共享 mock 状态（必须通过 vi.hoisted 提升，vi.mock 工厂才能引用到）
// ---------------------------------------------------------------------------
const M = vi.hoisted(() => {
  // 模拟窗口对象类型
  type MockWindow = {
    id: string;
    opts: Record<string, unknown>;
    webContents: { send: ReturnType<typeof vi.fn>; openDevTools: ReturnType<typeof vi.fn> };
    once: ReturnType<typeof vi.fn>;
    on: ReturnType<typeof vi.fn>;
    loadURL: ReturnType<typeof vi.fn>;
    loadFile: ReturnType<typeof vi.fn>;
    show: ReturnType<typeof vi.fn>;
    hide: ReturnType<typeof vi.fn>;
    focus: ReturnType<typeof vi.fn>;
    close: ReturnType<typeof vi.fn>;
    minimize: ReturnType<typeof vi.fn>;
    maximize: ReturnType<typeof vi.fn>;
    unmaximize: ReturnType<typeof vi.fn>;
    isMaximized: ReturnType<typeof vi.fn>;
    isVisible: ReturnType<typeof vi.fn>;
    isDestroyed: ReturnType<typeof vi.fn>;
    destroy: ReturnType<typeof vi.fn>;
  };

  const createdWindows: MockWindow[] = [];
  let windowCounter = 0;
  // 当前“活动”窗口，供 BrowserWindow.fromWebContents 返回
  let activeWindow: MockWindow | null = null;

  function makeWindow(opts: Record<string, unknown>): MockWindow {
    const w = {
      id: `win-${++windowCounter}`,
      opts,
      webContents: { send: vi.fn(), openDevTools: vi.fn() },
      once: vi.fn((event: string, cb: () => void) => {
        // ready-to-show 立即触发，便于 createMainWindow 完成 show 行为
        if (event === 'ready-to-show') cb();
        return w;
      }),
      on: vi.fn(() => w),
      loadURL: vi.fn(() => Promise.resolve()),
      loadFile: vi.fn(() => Promise.resolve()),
      show: vi.fn(),
      hide: vi.fn(),
      focus: vi.fn(),
      close: vi.fn(),
      minimize: vi.fn(),
      maximize: vi.fn(),
      unmaximize: vi.fn(),
      isMaximized: vi.fn(() => false),
      isVisible: vi.fn(() => true),
      isDestroyed: vi.fn(() => false),
      destroy: vi.fn(),
    } as MockWindow;
    createdWindows.push(w);
    activeWindow = w;
    return w;
  }

  const ipcHandlers = new Map<string, (...args: unknown[]) => unknown>();
  const ipcListeners = new Map<string, (...args: unknown[]) => unknown>();

  const appMock = {
    isPackaged: false,
    version: '0.1.0',
    getAppPath: vi.fn(() => '/fake/app'),
    getVersion: vi.fn(() => appMock.version),
    quit: vi.fn(),
    whenReady: vi.fn(() => new Promise<void>(() => {})), // 不自动 resolve，避免 bootstrap 自动运行
    on: vi.fn(),
  };

  const menuMock = {
    buildFromTemplate: vi.fn(
      <T,>(template: T) => ({ __template: template, __isMenu: true }),
    ),
    setApplicationMenu: vi.fn(),
  };

  const trayInstanceMock = {
    setToolTip: vi.fn(),
    setContextMenu: vi.fn(),
    on: vi.fn(),
    destroy: vi.fn(),
  };
  const TrayCtor = vi.fn(() => trayInstanceMock);

  const nativeImageMock = { createEmpty: vi.fn(() => ({ __empty: true })) };

  const nativeThemeMock = {
    themeSource: 'system' as 'dark' | 'light' | 'system',
  };

  const NotificationCtor = vi.fn(() => ({ show: vi.fn() }));

  const shellMock = { openExternal: vi.fn(() => Promise.resolve()) };

  const ipcMainMock = {
    handle: vi.fn((channel: string, handler: (...args: unknown[]) => unknown) => {
      ipcHandlers.set(channel, handler);
    }),
    on: vi.fn((channel: string, listener: (...args: unknown[]) => unknown) => {
      ipcListeners.set(channel, listener);
    }),
  };

  const contextBridgeMock = { exposeInMainWorld: vi.fn() };

  const ipcRendererMock = {
    invoke: vi.fn(() => Promise.resolve()),
    send: vi.fn(),
  };

  const BrowserWindowCtor = vi.fn((opts: Record<string, unknown>) => makeWindow(opts));
  const BrowserWindowStatic = {
    fromWebContents: vi.fn(() => activeWindow),
    getAllWindows: vi.fn(() => createdWindows),
  };
  // 合并 constructor 与静态方法
  const BrowserWindow = Object.assign(BrowserWindowCtor, BrowserWindowStatic);

  const autoUpdaterMock = {
    autoDownload: true,
    autoInstallOnAppQuit: true,
    on: vi.fn(),
    checkForUpdates: vi.fn(() => Promise.resolve(null)),
    downloadUpdate: vi.fn(() => Promise.resolve()),
    quitAndInstall: vi.fn(() => Promise.resolve()),
  };

  return {
    createdWindows,
    makeWindow,
    ipcHandlers,
    ipcListeners,
    app: appMock,
    Menu: menuMock,
    trayInstance: trayInstanceMock,
    Tray: TrayCtor,
    nativeImage: nativeImageMock,
    nativeTheme: nativeThemeMock,
    Notification: NotificationCtor,
    shell: shellMock,
    ipcMain: ipcMainMock,
    contextBridge: contextBridgeMock,
    ipcRenderer: ipcRendererMock,
    BrowserWindow,
    autoUpdater: autoUpdaterMock,
    setActiveWindow(w: MockWindow | null) {
      activeWindow = w;
    },
  };
});

// ---------------------------------------------------------------------------
// vi.mock 必须在顶层；工厂内引用 hoisted 状态 M
// ---------------------------------------------------------------------------
vi.mock('electron', () => ({
  app: M.app,
  BrowserWindow: M.BrowserWindow,
  Menu: M.Menu,
  Tray: M.Tray,
  nativeImage: M.nativeImage,
  nativeTheme: M.nativeTheme,
  Notification: M.Notification,
  shell: M.shell,
  ipcMain: M.ipcMain,
  contextBridge: M.contextBridge,
  ipcRenderer: M.ipcRenderer,
}));

vi.mock('electron-updater', () => ({
  autoUpdater: M.autoUpdater,
}));

// 测试中通过动态导入延迟加载被测模块，便于在 beforeEach 内重置 mock 状态。
let windowModule: typeof import('../electron/window');
let preloadModule: typeof import('../electron/preload');
let ipcModule: typeof import('../electron/ipc-handlers');
let menuModule: typeof import('../electron/menu');
let trayModule: typeof import('../electron/tray');
let updaterModule: typeof import('../electron/updater');
let mainModule: typeof import('../electron/main');

async function loadModules(): Promise<void> {
  // vi.resetModules 确保 import 时重新执行模块代码（preload 的 exposeWorkamaApi 等）
  vi.resetModules();
  windowModule = await import('../electron/window');
  preloadModule = await import('../electron/preload');
  ipcModule = await import('../electron/ipc-handlers');
  menuModule = await import('../electron/menu');
  trayModule = await import('../electron/tray');
  updaterModule = await import('../electron/updater');
  mainModule = await import('../electron/main');
}

// 收集到的 mock 调用需要 beforeEach 重置
function resetAllMocks(): void {
  M.createdWindows.length = 0;
  M.ipcHandlers.clear();
  M.ipcListeners.clear();
  M.app.getAppPath.mockClear();
  M.app.getVersion.mockClear();
  M.app.quit.mockClear();
  M.app.whenReady.mockClear();
  M.app.on.mockClear();
  M.app.isPackaged = false;
  M.app.version = '0.1.0';
  M.Menu.buildFromTemplate.mockClear();
  M.Menu.setApplicationMenu.mockClear();
  M.Tray.mockClear();
  M.trayInstance.setToolTip.mockClear();
  M.trayInstance.setContextMenu.mockClear();
  M.trayInstance.on.mockClear();
  M.trayInstance.destroy.mockClear();
  M.nativeImage.createEmpty.mockClear();
  M.Notification.mockClear();
  M.shell.openExternal.mockClear();
  M.ipcMain.handle.mockClear();
  M.ipcMain.on.mockClear();
  M.contextBridge.exposeInMainWorld.mockClear();
  M.ipcRenderer.invoke.mockClear();
  M.ipcRenderer.send.mockClear();
  M.BrowserWindow.mockClear();
  (M.BrowserWindow.fromWebContents as ReturnType<typeof vi.fn>).mockClear();
  (M.BrowserWindow.getAllWindows as ReturnType<typeof vi.fn>).mockClear();
  M.autoUpdater.on.mockClear();
  M.autoUpdater.checkForUpdates.mockClear();
  M.autoUpdater.downloadUpdate.mockClear();
  M.autoUpdater.quitAndInstall.mockClear();
  M.autoUpdater.autoDownload = true;
  M.autoUpdater.autoInstallOnAppQuit = true;
  M.nativeTheme.themeSource = 'system';
}

beforeEach(async () => {
  resetAllMocks();
  await loadModules();
});

// ---------------------------------------------------------------------------
// 1. window.ts — 窗口创建与配置
// ---------------------------------------------------------------------------
describe('window.ts', () => {
  it('createMainWindow 创建 BrowserWindow 并设置正确尺寸', async () => {
    const win = await windowModule.createMainWindow();
    expect(M.BrowserWindow).toHaveBeenCalledTimes(1);
    const opts = (M.BrowserWindow as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(opts.width).toBe(1280);
    expect(opts.height).toBe(800);
    expect(opts.minWidth).toBe(1024);
    expect(opts.minHeight).toBe(600);
    expect(opts.title).toBe('WorkAMA');
    expect(win).toBeDefined();
  });

  it('createMainWindow 启用 contextIsolation / 关闭 nodeIntegration / 开启 sandbox', async () => {
    await windowModule.createMainWindow();
    const opts = (M.BrowserWindow as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as {
      webPreferences: Record<string, unknown>;
    };
    expect(opts.webPreferences.contextIsolation).toBe(true);
    expect(opts.webPreferences.nodeIntegration).toBe(false);
    expect(opts.webPreferences.sandbox).toBe(true);
  });

  it('createMainWindow 在开发态加载 DEV_URL 并打开 DevTools', async () => {
    M.app.isPackaged = false;
    const win = await windowModule.createMainWindow();
    expect(win.loadURL).toHaveBeenCalledWith('http://localhost:20204');
    expect(win.webContents.openDevTools).toHaveBeenCalled();
  });

  it('createMainWindow 在生产态加载本地 renderer 文件', async () => {
    M.app.isPackaged = true;
    // 需要重新加载模块使 isDev 在 window.ts 内重新求值
    vi.resetModules();
    windowModule = await import('../electron/window');
    const win = await windowModule.createMainWindow();
    expect(win.loadFile).toHaveBeenCalled();
    expect(win.loadURL).not.toHaveBeenCalled();
  });

  it('createSettingsWindow 复用已存在的窗口', () => {
    const first = windowModule.createSettingsWindow();
    const second = windowModule.createSettingsWindow();
    expect(second).toBe(first);
    // 第二次调用应仅触发 focus（不重复 new BrowserWindow 用于 settings）
    expect(first.focus).toHaveBeenCalled();
  });

  it('createAboutWindow 复用已存在的窗口', () => {
    const first = windowModule.createAboutWindow();
    const second = windowModule.createAboutWindow();
    expect(second).toBe(first);
    expect(first.focus).toHaveBeenCalled();
  });

  it('getWindowById 返回已登记的窗口', async () => {
    await windowModule.createMainWindow();
    expect(windowModule.getWindowById('main')).toBeDefined();
    expect(windowModule.getWindowById('non-exist')).toBeUndefined();
  });

  it('closeWindowById 关闭已登记窗口并返回 true；未登记返回 false', async () => {
    await windowModule.createMainWindow();
    expect(windowModule.closeWindowById('main')).toBe(true);
    expect(windowModule.closeWindowById('non-exist')).toBe(false);
  });

  it('getAllDesktopWindows 返回全部已创建窗口数组', async () => {
    await windowModule.createMainWindow();
    windowModule.createSettingsWindow();
    expect(windowModule.getAllDesktopWindows().length).toBeGreaterThanOrEqual(2);
  });
});

// ---------------------------------------------------------------------------
// 2. preload.ts — contextBridge 安全暴露 API
// ---------------------------------------------------------------------------
describe('preload.ts', () => {
  it('exposeWorkamaApi 通过 contextBridge 在 window 上暴露 workama 命名空间', () => {
    expect(M.contextBridge.exposeInMainWorld).toHaveBeenCalledWith('workama', expect.any(Object));
  });

  it('api.login 调用 ipcRenderer.invoke("auth:login", email, password)', () => {
    const api = M.contextBridge.exposeInMainWorld.mock.calls[0][1] as {
      api: { login: (e: string, p: string) => Promise<unknown> };
    };
    void api.api.login('a@b.com', 'pwd');
    expect(M.ipcRenderer.invoke).toHaveBeenCalledWith('auth:login', 'a@b.com', 'pwd');
  });

  it('api.logout 调用 ipcRenderer.send("auth:logout")', () => {
    const api = M.contextBridge.exposeInMainWorld.mock.calls[0][1] as {
      api: { logout: () => void };
    };
    api.api.logout();
    expect(M.ipcRenderer.send).toHaveBeenCalledWith('auth:logout');
  });

  it('window.close 调用 ipcRenderer.send("window:close")', () => {
    const api = M.contextBridge.exposeInMainWorld.mock.calls[0][1] as {
      window: { close: () => void };
    };
    api.window.close();
    expect(M.ipcRenderer.send).toHaveBeenCalledWith('window:close');
  });

  it('system.openExternal 调用 ipcRenderer.send("system:openExternal", url)', () => {
    const api = M.contextBridge.exposeInMainWorld.mock.calls[0][1] as {
      system: { openExternal: (url: string) => void };
    };
    api.system.openExternal('https://workama.ai');
    expect(M.ipcRenderer.send).toHaveBeenCalledWith('system:openExternal', 'https://workama.ai');
  });
});

// ---------------------------------------------------------------------------
// 3. ipc-handlers.ts — IPC 通道与业务逻辑
// ---------------------------------------------------------------------------
describe('ipc-handlers.ts', () => {
  it('login 在 email/password 为空时返回失败', async () => {
    const r1 = await ipcModule.login('', 'pwd');
    expect(r1.success).toBe(false);
    expect(r1.error).toMatch(/required/);
    const r2 = await ipcModule.login('a@b.com', '');
    expect(r2.success).toBe(false);
  });

  it('login 成功时存储 token 并返回 success', async () => {
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) =>
          resolve({
            ok: true,
            json: () =>
              Promise.resolve({ access_token: 'tok-123', user: { id: 1 } }),
          } as Response),
        ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const r = await ipcModule.login('a@b.com', 'pwd');
    expect(r.success).toBe(true);
    expect(r.token).toBe('tok-123');
    expect(ipcModule.getAuthToken()).toBe('tok-123');
    vi.unstubAllGlobals();
  });

  it('apiCall 在 path 为空时返回失败', async () => {
    const r = await ipcModule.apiCall('GET', '');
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/required/);
  });

  it('apiCall 在已登录时携带 Authorization 头', async () => {
    ipcModule.setAuthToken('tok-abc');
    let capturedInit: RequestInit | undefined;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      capturedInit = init;
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{"x":1}') } as unknown as Response);
    });
    vi.stubGlobal('fetch', fetchMock);
    await ipcModule.apiCall('GET', '/api/v1/me');
    expect((capturedInit?.headers as Record<string, string>)?.Authorization).toBe(
      'Bearer tok-abc',
    );
    vi.unstubAllGlobals();
  });

  it('setAuthToken / getAuthToken 互配读写', () => {
    ipcModule.setAuthToken('hello');
    expect(ipcModule.getAuthToken()).toBe('hello');
    ipcModule.setAuthToken(null);
    expect(ipcModule.getAuthToken()).toBeNull();
  });

  it('showNotification 在标题为空时返回 false', () => {
    expect(ipcModule.showNotification('', 'body')).toBe(false);
  });

  it('showNotification 创建 Notification 并调用 show', () => {
    const r = ipcModule.showNotification('hi', 'body');
    expect(r).toBe(true);
    expect(M.Notification).toHaveBeenCalled();
  });

  it('openExternal 在 url 为空时返回 false', () => {
    expect(ipcModule.openExternal('')).toBe(false);
  });

  it('openExternal 在 url 非空时调用 shell.openExternal 并返回 true', () => {
    const r = ipcModule.openExternal('https://workama.ai');
    expect(r).toBe(true);
    expect(M.shell.openExternal).toHaveBeenCalledWith('https://workama.ai');
  });

  it('getAppVersion 返回 app.getVersion() 结果', () => {
    expect(ipcModule.getAppVersion()).toBe('0.1.0');
    expect(M.app.getVersion).toHaveBeenCalled();
  });

  it('getTheme / setTheme 操作 nativeTheme.themeSource', () => {
    ipcModule.setTheme('dark');
    expect(M.nativeTheme.themeSource).toBe('dark');
    expect(ipcModule.getTheme()).toBe('dark');
  });

  it('registerIpcHandlers 注册了 auth:login / api:call / theme:get / window:* 等通道', () => {
    ipcModule.registerIpcHandlers();
    expect(M.ipcMain.handle).toHaveBeenCalledWith('auth:login', expect.any(Function));
    expect(M.ipcMain.handle).toHaveBeenCalledWith('api:call', expect.any(Function));
    expect(M.ipcMain.handle).toHaveBeenCalledWith('auth:getToken', expect.any(Function));
    expect(M.ipcMain.handle).toHaveBeenCalledWith('system:getVersion', expect.any(Function));
    expect(M.ipcMain.handle).toHaveBeenCalledWith('theme:get', expect.any(Function));
    expect(M.ipcMain.on).toHaveBeenCalledWith('auth:logout', expect.any(Function));
    expect(M.ipcMain.on).toHaveBeenCalledWith('window:minimize', expect.any(Function));
    expect(M.ipcMain.on).toHaveBeenCalledWith('window:maximize', expect.any(Function));
    expect(M.ipcMain.on).toHaveBeenCalledWith('window:close', expect.any(Function));
    expect(M.ipcMain.on).toHaveBeenCalledWith('theme:set', expect.any(Function));
  });

  it('auth:logout listener 清空 authToken', async () => {
    ipcModule.registerIpcHandlers();
    ipcModule.setAuthToken('temp');
    const handler = M.ipcListeners.get('auth:logout');
    expect(handler).toBeDefined();
    handler?.({} as unknown, ...[]);
    expect(ipcModule.getAuthToken()).toBeNull();
  });

  it('window:minimize listener 调用 BrowserWindow.fromWebContents().minimize()', () => {
    ipcModule.registerIpcHandlers();
    const minimizeHandler = M.ipcListeners.get('window:minimize');
    expect(minimizeHandler).toBeDefined();
    minimizeHandler?.({ sender: 'fake-wc' } as unknown, ...[]);
    expect(M.BrowserWindow.fromWebContents).toHaveBeenCalledWith('fake-wc');
  });
});

// ---------------------------------------------------------------------------
// 4. menu.ts — 应用菜单
// ---------------------------------------------------------------------------
describe('menu.ts', () => {
  it('buildMenu 构建 Menu 并调用 setApplicationMenu', () => {
    const menu = menuModule.buildMenu();
    expect(M.Menu.buildFromTemplate).toHaveBeenCalledOnce();
    expect(M.Menu.setApplicationMenu).toHaveBeenCalledWith(menu);
  });

  it('buildMenu 模板包含 文件/编辑/视图/窗口/帮助 五个顶层菜单', () => {
    menuModule.buildMenu();
    const template = M.Menu.buildFromTemplate.mock.calls[0][0] as Array<{
      label: string;
      submenu: unknown[];
    }>;
    const labels = template.map((t) => t.label);
    expect(labels).toEqual(['文件', '编辑', '视图', '窗口', '帮助']);
  });

  it('buildMenu 视图菜单包含主题切换子菜单（浅色/深色/跟随系统）', () => {
    menuModule.buildMenu();
    const template = M.Menu.buildFromTemplate.mock.calls[0][0] as Array<{
      label: string;
      submenu: Array<{ label: string; click?: () => void }>;
    }>;
    const viewMenu = template.find((t) => t.label === '视图');
    expect(viewMenu).toBeDefined();
    const themeItem = viewMenu?.submenu.find((i) => i.label === '切换主题');
    expect(themeItem).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// 5. tray.ts — 系统托盘
// ---------------------------------------------------------------------------
describe('tray.ts', () => {
  it('createTray 创建 Tray 并设置 ToolTip 为 WorkAMA', () => {
    const fakeWin = M.makeWindow({}) as never;
    trayModule.createTray(fakeWin);
    expect(M.Tray).toHaveBeenCalledOnce();
    expect(M.trayInstance.setToolTip).toHaveBeenCalledWith('WorkAMA');
    expect(M.trayInstance.setContextMenu).toHaveBeenCalled();
  });

  it('createTray 注册 click 事件以切换窗口可见性', () => {
    const fakeWin = M.makeWindow({}) as never;
    trayModule.createTray(fakeWin);
    expect(M.trayInstance.on).toHaveBeenCalledWith('click', expect.any(Function));
  });

  it('getTray 在 createTray 后返回实例；destroyTray 销毁后返回 null', () => {
    const fakeWin = M.makeWindow({}) as never;
    trayModule.createTray(fakeWin);
    expect(trayModule.getTray()).toBe(M.trayInstance);
    trayModule.destroyTray();
    expect(trayModule.getTray()).toBeNull();
    expect(M.trayInstance.destroy).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 6. updater.ts — 自动更新
// ---------------------------------------------------------------------------
describe('updater.ts', () => {
  it('initAutoUpdater 设置 autoDownload=false 与 autoInstallOnAppQuit=true', () => {
    const fakeWin = M.makeWindow({}) as never;
    updaterModule.initAutoUpdater(fakeWin);
    expect(M.autoUpdater.autoDownload).toBe(false);
    expect(M.autoUpdater.autoInstallOnAppQuit).toBe(true);
  });

  it('initAutoUpdater 注册 update-available / update-downloaded / error 监听器', () => {
    const fakeWin = M.makeWindow({}) as never;
    updaterModule.initAutoUpdater(fakeWin);
    const channels = M.autoUpdater.on.mock.calls.map((c) => c[0]);
    expect(channels).toContain('update-available');
    expect(channels).toContain('update-downloaded');
    expect(channels).toContain('error');
  });

  it('checkForUpdates 在 autoUpdater 返回 null 时解析为 false', async () => {
    M.autoUpdater.checkForUpdates.mockResolvedValueOnce(null);
    const r = await updaterModule.checkForUpdates();
    expect(r).toBe(false);
  });

  it('checkForUpdates 在 autoUpdater 抛错时捕获并返回 false', async () => {
    M.autoUpdater.checkForUpdates.mockRejectedValueOnce(new Error('network'));
    const r = await updaterModule.checkForUpdates();
    expect(r).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 7. main.ts — 主进程装配
// ---------------------------------------------------------------------------
describe('main.ts', () => {
  it('bootstrap 调用 registerIpcHandlers + createMainWindow + buildMenu + createTray + initAutoUpdater', async () => {
    // 临时让 whenReady 不再阻塞（已 import 完成，bootstrap 直接调用即可）
    const win = await mainModule.bootstrap();
    expect(win).toBeDefined();
    // registerIpcHandlers 注册了通道
    expect(M.ipcMain.handle).toHaveBeenCalledWith('auth:login', expect.any(Function));
    // buildMenu 调用了 setApplicationMenu
    expect(M.Menu.setApplicationMenu).toHaveBeenCalled();
    // createTray 创建了 Tray
    expect(M.Tray).toHaveBeenCalled();
    // initAutoUpdater 设置了 autoDownload=false
    expect(M.autoUpdater.autoDownload).toBe(false);
  });

  it('getMainWindow 在 bootstrap 后返回主窗口，调用前为 null', async () => {
    expect(mainModule.getMainWindow()).toBeNull();
    await mainModule.bootstrap();
    expect(mainModule.getMainWindow()).not.toBeNull();
  });
});
