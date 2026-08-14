import { contextBridge, ipcRenderer } from 'electron';

// 预加载脚本：在 contextIsolation 开启的前提下，通过 contextBridge 向渲染进程安全暴露
// 受控的 `window.workama` API。仅暴露白名单 IPC 通道（invoke/send），不暴露 ipcRenderer 本身，
// 渲染进程无法直接发起任意 IPC 调用或访问 Node 能力（安全模型见《410》§5.3）。
//
// 暴露三组能力：
//   - api：登录、API 调用、读取/清除 token（invoke + send）
//   - system：系统通知、打开外链、读取平台与版本
//   - window：最小化/最大化/关闭当前窗口

export interface WorkamaApi {
  api: {
    login: (email: string, password: string) => Promise<unknown>;
    call: (method: string, path: string, body?: unknown) => Promise<unknown>;
    getToken: () => Promise<string | null>;
    logout: () => void;
  };
  system: {
    showNotification: (title: string, body: string) => void;
    openExternal: (url: string) => void;
    getPlatform: () => NodeJS.Platform;
    getVersion: () => Promise<string>;
  };
  window: {
    minimize: () => void;
    maximize: () => void;
    close: () => void;
  };
}

export function exposeWorkamaApi(): WorkamaApi {
  const api: WorkamaApi = {
    api: {
      login: (email, password) => ipcRenderer.invoke('auth:login', email, password),
      call: (method, p, body) => ipcRenderer.invoke('api:call', method, p, body),
      getToken: () => ipcRenderer.invoke('auth:getToken'),
      logout: () => ipcRenderer.send('auth:logout'),
    },
    system: {
      showNotification: (title, body) => ipcRenderer.send('notification:show', title, body),
      openExternal: (url) => ipcRenderer.send('system:openExternal', url),
      getPlatform: () => process.platform,
      getVersion: () => ipcRenderer.invoke('system:getVersion'),
    },
    window: {
      minimize: () => ipcRenderer.send('window:minimize'),
      maximize: () => ipcRenderer.send('window:maximize'),
      close: () => ipcRenderer.send('window:close'),
    },
  };
  contextBridge.exposeInMainWorld('workama', api);
  return api;
}

exposeWorkamaApi();
