import { app, BrowserWindow, ipcMain, nativeTheme, Notification, shell } from 'electron';
import { createAboutWindow, createSettingsWindow } from './window';

// IPC 处理器：集中注册主进程对外暴露的 IPC 通道。
//
// 认证与 API 调用走主进程 fetch（不把 token 暴露到渲染进程的 localStorage），
// 平台 API 基址通过 WORKAMA_PLATFORM_API_URL 注入，缺省回落到本地 platform-api（http://localhost:20200）。
// 鉴权端点与 apps/web 保持一致：/api/v1/auth/login、/api/v1/auth/me（契约见《700》）。

const PLATFORM_API_URL = process.env.WORKAMA_PLATFORM_API_URL ?? 'http://localhost:20200';
const LOGIN_PATH = '/api/v1/auth/login';

let authToken: string | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
}

export function getAuthToken(): string | null {
  return authToken;
}

export interface LoginResult {
  success: boolean;
  token?: string;
  user?: unknown;
  error?: string;
}

export async function login(email: string, password: string): Promise<LoginResult> {
  if (!email || !password) {
    return { success: false, error: 'email and password are required' };
  }
  try {
    const res = await fetch(new URL(LOGIN_PATH, PLATFORM_API_URL).toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      return { success: false, error: `login failed: ${res.status}` };
    }
    const payload = (await res.json()) as { access_token?: string; user?: unknown };
    if (!payload.access_token) {
      return { success: false, error: 'no access_token in response' };
    }
    authToken = payload.access_token;
    return { success: true, token: payload.access_token, user: payload.user };
  } catch (err) {
    return { success: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export interface ApiCallResult {
  ok: boolean;
  status: number;
  data?: unknown;
  error?: string;
}

export async function apiCall(method: string, p: string, body?: unknown): Promise<ApiCallResult> {
  const methodUpper = method.toUpperCase();
  if (!p) {
    return { ok: false, status: 0, error: 'path is required' };
  }
  try {
    const init: RequestInit = {
      method: methodUpper,
      headers: {
        'Content-Type': 'application/json',
        ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
      },
    };
    if (body !== undefined && methodUpper !== 'GET' && methodUpper !== 'HEAD') {
      init.body = JSON.stringify(body ?? {});
    }
    const res = await fetch(new URL(p, PLATFORM_API_URL).toString(), init);
    const text = await res.text();
    let data: unknown = undefined;
    try {
      data = text ? JSON.parse(text) : undefined;
    } catch {
      data = text;
    }
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, error: err instanceof Error ? err.message : String(err) };
  }
}

export function showNotification(title: string, body: string): boolean {
  if (!title) return false;
  const n = new Notification({ title, body });
  n.show();
  return true;
}

export function openExternal(url: string): boolean {
  if (!url) return false;
  void shell.openExternal(url);
  return true;
}

export function getAppVersion(): string {
  return app.getVersion();
}

export type ThemeMode = 'dark' | 'light' | 'system';

export function getTheme(): ThemeMode {
  return nativeTheme.themeSource;
}

export function setTheme(theme: ThemeMode): void {
  nativeTheme.themeSource = theme;
}

export function registerIpcHandlers(): void {
  ipcMain.handle('auth:login', (_e, email: string, password: string) => login(email, password));
  ipcMain.handle('auth:getToken', () => authToken);
  ipcMain.on('auth:logout', () => {
    authToken = null;
  });
  ipcMain.handle('api:call', (_e, method: string, p: string, body?: unknown) =>
    apiCall(method, p, body),
  );

  ipcMain.on('notification:show', (_e, title: string, body: string) => showNotification(title, body));
  ipcMain.on('system:openExternal', (_e, url: string) => openExternal(url));
  ipcMain.handle('system:getVersion', () => getAppVersion());

  ipcMain.handle('theme:get', () => getTheme());
  ipcMain.on('theme:set', (_e, theme: ThemeMode) => setTheme(theme));

  ipcMain.on('window:minimize', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.minimize();
  });
  ipcMain.on('window:maximize', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!w) return;
    if (w.isMaximized()) w.unmaximize();
    else w.maximize();
  });
  ipcMain.on('window:close', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.close();
  });

  ipcMain.on('window:open-settings', () => {
    createSettingsWindow();
  });
  ipcMain.on('window:open-about', () => {
    createAboutWindow();
  });
}
