// background.ts 单元测试
// 覆盖：workamaApiCall / handleLogin / handleChat / handleSaveToKnowledge /
//       dispatchMessage 路由 / runtime.onMessage 与 action.onClicked 事件注册

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  workamaApiCall,
  handleLogin,
  handleChat,
  handleSaveToKnowledge,
  dispatchMessage,
  setAuthToken,
} from '../src/background';

// 通过 setup.ts 注入的全局 mock 引用
const chromeMock = (globalThis as unknown as { __chromeMock: any }).__chromeMock;
const fetchMock = (globalThis as unknown as { __fetchMock: any }).__fetchMock;

function lastFetchCall(): [string, RequestInit] {
  return fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
}

describe('background - workamaApiCall', () => {
  beforeEach(() => {
    fetchMock.mockClear();
  });

  it('GET 请求且无 body 时不附带请求体但携带 Content-Type', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    });
    await workamaApiCall('/api/v1/health', 'GET');
    const [url, init] = lastFetchCall();
    expect(url).toBe('http://localhost:20200/api/v1/health');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBe('application/json');
  });

  it('存在 token 时附带 Authorization Bearer 头', async () => {
    await setAuthToken('secret-token');
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    });
    await workamaApiCall('/api/v1/me', 'GET');
    const [, init] = lastFetchCall();
    expect(init.headers['Authorization']).toBe('Bearer secret-token');
  });

  it('提供 body 时序列化为 JSON 并保持 Content-Type', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    });
    await workamaApiCall('/api/v1/items', 'POST', { name: 'foo' });
    const [, init] = lastFetchCall();
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ name: 'foo' }));
    expect(init.headers['Content-Type']).toBe('application/json');
  });

  it('响应非 ok 时抛出包含状态码的错误', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => ({}),
    });
    await expect(workamaApiCall('/api/v1/fail', 'GET')).rejects.toThrow(
      'WorkAMA API error: 500 Internal Server Error',
    );
  });
});

describe('background - 消息处理器', () => {
  beforeEach(() => {
    fetchMock.mockClear();
  });

  it('handleLogin 调用登录接口并保存返回的 token', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ token: 'tok-login' }),
    });
    const result = await handleLogin({ email: 'a@b.com', password: 'pass' });
    expect(result.token).toBe('tok-login');
    const [url, init] = lastFetchCall();
    expect(url).toBe('http://localhost:20200/api/v1/auth/login');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ email: 'a@b.com', password: 'pass' }));
    expect(chromeMock.storage.local._data.workama_token).toBe('tok-login');
  });

  it('handleChat 向 /api/v1/chat 发送 POST', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ reply: 'hi' }),
    });
    await handleChat({ message: '你好' });
    const [url, init] = lastFetchCall();
    expect(url).toBe('http://localhost:20200/api/v1/chat');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ message: '你好' }));
  });

  it('handleSaveToKnowledge 向知识库接口保存负载', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ id: 'k1' }),
    });
    const payload = { title: 'T', content: 'C', source: 'https://example.com' };
    await handleSaveToKnowledge(payload);
    const [url, init] = lastFetchCall();
    expect(url).toBe('http://localhost:20200/api/v1/knowledge');
    expect(init.body).toBe(JSON.stringify(payload));
  });
});

describe('background - dispatchMessage 路由', () => {
  beforeEach(() => {
    fetchMock.mockClear();
  });

  it('LOGIN 消息被路由到 handleLogin', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ token: 't' }),
    });
    const data = await dispatchMessage({
      type: 'LOGIN',
      payload: { email: 'a@b.com', password: 'p' },
    });
    expect(data).toEqual({ token: 't' });
  });

  it('EXTRACT_PAGE_CONTENT 消息直接回传 payload', async () => {
    const payload = { url: 'https://x.com', title: 'X', text: 'hello', selection: 'hello' };
    const data = await dispatchMessage({ type: 'EXTRACT_PAGE_CONTENT', payload });
    expect(data).toEqual(payload);
  });

  it('未知消息类型抛出错误', async () => {
    await expect(
      dispatchMessage({ type: 'UNKNOWN' as never, payload: {} }),
    ).rejects.toThrow('Unknown message type: UNKNOWN');
  });
});

describe('background - 事件注册', () => {
  beforeEach(() => {
    fetchMock.mockClear();
  });

  it('runtime.onMessage 已注册监听器', () => {
    expect(chromeMock.runtime.onMessage._listeners.length).toBeGreaterThan(0);
  });

  it('onMessage 监听器处理 LOGIN 并以 ok:true 调用 sendResponse', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ token: 'evt-tok' }),
    });
    const sendResponse = vi.fn();
    const listener = chromeMock.runtime.onMessage._listeners[0];
    const result = listener(
      { type: 'LOGIN', payload: { email: 'a@b.com', password: 'p' } },
      {},
      sendResponse,
    );
    expect(result).toBe(true);
    await vi.waitFor(() => expect(sendResponse).toHaveBeenCalled());
    expect(sendResponse).toHaveBeenCalledWith({ ok: true, data: { token: 'evt-tok' } });
  });

  it('action.onClicked 打开 sidePanel 并传入 tabId', async () => {
    const listeners = chromeMock.action.onClicked._listeners;
    expect(listeners.length).toBeGreaterThan(0);
    await listeners[0]({ id: 42 });
    expect(chromeMock.sidePanel.open).toHaveBeenCalledWith({ tabId: 42 });
  });
});
