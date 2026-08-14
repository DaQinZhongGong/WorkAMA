import type { WorkamaApi } from '../electron/preload';

// 渲染进程全局类型声明：preload 通过 contextBridge 注入 window.workama。
// 此处仅声明类型，不引入运行时依赖（import type 在编译期擦除）。

declare global {
  interface Window {
    workama: WorkamaApi;
  }
}

export {};
