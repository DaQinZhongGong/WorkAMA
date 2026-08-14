// Vitest 全局测试设置
// 提供 chrome.* API 的 mock 与全局 fetch mock，
// 供 background / content / popup 测试统一使用。

import '@testing-library/jest-dom';
import { vi, beforeEach } from 'vitest';

type Listener = (...args: unknown[]) => unknown;

/** 事件中心：模拟 chrome.*.onXxx.addListener */
interface EventHub {
  addListener: ReturnType<typeof vi.fn>;
  _listeners: Listener[];
}

function makeEvent(): EventHub {
  const listeners: Listener[] = [];
  return {
    addListener: vi.fn((l: Listener) => {
      listeners.push(l);
    }),
    _listeners: listeners,
  };
}

// chrome.storage.local 的内存实现
const storageData: Record<string, unknown> = {};

const chromeMock = {
  runtime: {
    onMessage: makeEvent(),
    onInstalled: makeEvent(),
    sendMessage: vi.fn(async () => undefined) as ReturnType<typeof vi.fn>,
    id: 'workama-test-extension',
    lastError: null,
  },
  storage: {
    local: {
      get: vi.fn(async (keys?: unknown) => {
        const result: Record<string, unknown> = {};
        if (keys === undefined || keys === null) {
          Object.assign(result, storageData);
        } else if (typeof keys === 'string') {
          if (keys in storageData) {
            result[keys] = storageData[keys];
          }
        } else if (Array.isArray(keys)) {
          for (const k of keys) {
            if (k in storageData) {
              result[k as string] = storageData[k as string];
            }
          }
        } else if (typeof keys === 'object') {
          for (const k of Object.keys(keys as Record<string, unknown>)) {
            result[k] = k in storageData ? storageData[k] : (keys as Record<string, unknown>)[k];
          }
        }
        return result;
      }),
      set: vi.fn(async (items: Record<string, unknown>) => {
        Object.assign(storageData, items);
      }),
      _data: storageData,
    },
  },
  action: {
    onClicked: makeEvent(),
  },
  contextMenus: {
    create: vi.fn(() => undefined),
    onClicked: makeEvent(),
  },
  sidePanel: {
    open: vi.fn(async () => undefined),
  },
  tabs: {
    query: vi.fn(async () => [{ id: 1, url: 'https://example.com' }]),
  },
};

// 暴露给被测模块（background/content/popup 顶层引用 chrome.*）
;(globalThis as unknown as { chrome: typeof chromeMock }).chrome = chromeMock;

// 全局 fetch mock，默认返回 200 空 JSON
const defaultFetchResponse = {
  ok: true,
  status: 200,
  statusText: 'OK',
  json: async () => ({}),
};

const fetchMock = vi.fn(async () => defaultFetchResponse);
(globalThis as unknown as { fetch: typeof fetchMock }).fetch = fetchMock;

// 暴露引用以便测试文件按需断言
;(globalThis as unknown as { __chromeMock: typeof chromeMock; __fetchMock: typeof fetchMock }).__chromeMock = chromeMock;
;(globalThis as unknown as { __fetchMock: typeof fetchMock }).__fetchMock = fetchMock;

beforeEach(() => {
  // 重置存储
  for (const k of Object.keys(storageData)) {
    delete storageData[k];
  }
  // 清除各 mock 的调用记录（保留实现）
  chromeMock.runtime.sendMessage.mockClear();
  chromeMock.storage.local.get.mockClear();
  chromeMock.storage.local.set.mockClear();
  chromeMock.sidePanel.open.mockClear();
  chromeMock.contextMenus.create.mockClear();
  chromeMock.tabs.query.mockClear();
  fetchMock.mockClear();
  // 恢复 fetch 默认实现
  fetchMock.mockImplementation(async () => defaultFetchResponse);
});
