// AMA-Work 桌面渲染进程精简壳。
//
// 设计说明（参见《320》§3/§4.2）：
// - 开发态主窗口实际加载 apps/web 开发服务器（http://localhost:20204）的完整 React 应用，
//   本文件仅作为生产态打包兜底与最小可用壳，复用 apps/web 的鉴权与 API 调用模式（契约一致）。
// - 所有平台能力通过 preload 暴露的 window.workama API 调用，渲染进程本身不持有 Node 能力。
// - 登录成功后通过 IPC 调用主进程 fetch 维持会话，token 不落 localStorage（与 web 端 sessionStorage 策略对齐）。

function el(id: string): HTMLElement | null {
  return document.getElementById(id);
}

function setStatus(text: string): void {
  const node = el('status');
  if (node) node.textContent = text;
}

async function bootstrap(): Promise<void> {
  try {
    const version = await window.workama.system.getVersion();
    setStatus(`WorkAMA 桌面客户端已就绪（v${version}）`);
  } catch {
    setStatus('WorkAMA 桌面客户端已就绪');
  }
}

interface LoginReply {
  success: boolean;
  error?: string;
}

function isLoginReply(value: unknown): value is LoginReply {
  return typeof value === 'object' && value !== null && 'success' in value;
}

async function onLogin(): Promise<void> {
  const email = (el('email') as HTMLInputElement | null)?.value ?? '';
  const password = (el('password') as HTMLInputElement | null)?.value ?? '';
  setStatus('正在登录…');
  const result = await window.workama.api.login(email, password);
  if (isLoginReply(result) && result.success) {
    setStatus('登录成功');
  } else {
    setStatus(isLoginReply(result) && result.error ? `登录失败：${result.error}` : '登录失败');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  void bootstrap();
  el('login')?.addEventListener('click', () => void onLogin());
  el('minimize')?.addEventListener('click', () => window.workama.window.minimize());
  el('close')?.addEventListener('click', () => window.workama.window.close());
  el('open-external')?.addEventListener('click', () =>
    window.workama.system.openExternal('https://workama.ai'),
  );
});
