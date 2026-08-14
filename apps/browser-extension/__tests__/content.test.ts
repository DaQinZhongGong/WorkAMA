// content.ts 单元测试
// 覆盖：extractPageContent / 右键菜单 handleContextMenuClick / onMessage 监听

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { extractPageContent, handleContextMenuClick } from '../src/content';

const chromeMock = (globalThis as unknown as { __chromeMock: any }).__chromeMock;

describe('content - extractPageContent', () => {
  it('返回页面标题、URL 与正文文本', () => {
    const doc = {
      title: '测试页面',
      body: { innerText: '这是正文内容' },
      getSelection: () => ({ toString: () => '' }),
    } as unknown as Document;
    const content = extractPageContent(doc);
    expect(content.title).toBe('测试页面');
    expect(content.text).toBe('这是正文内容');
    expect(content.selection).toBe('');
    expect(content.url).toBe(location.href);
  });

  it('存在选中文本时优先返回 selection', () => {
    const doc = {
      title: '带选区页面',
      body: { innerText: '正文全文' },
      getSelection: () => ({ toString: () => '选中的文字' }),
    } as unknown as Document;
    const content = extractPageContent(doc);
    expect(content.selection).toBe('选中的文字');
    expect(content.text).toBe('选中的文字');
  });
});

describe('content - 右键菜单', () => {
  const originalGetSelection = document.getSelection?.bind(document);
  const originalTitle = document.title;

  beforeEach(() => {
    chromeMock.runtime.sendMessage.mockClear();
  });

  afterEach(() => {
    // 还原 document 的修改
    if (originalGetSelection) {
      (document as unknown as { getSelection: unknown }).getSelection = originalGetSelection;
    }
    document.title = originalTitle;
  });

  it('handleContextMenuClick 发送 SAVE_TO_KNOWLEDGE 消息包含选中文本', () => {
    (document as unknown as { getSelection: unknown }).getSelection = () => ({
      toString: () => '右键选中文本',
    });
    document.title = '页面标题';
    handleContextMenuClick({ selectionText: '右键选中文本', menuItemId: 'send-to-workama' });
    expect(chromeMock.runtime.sendMessage).toHaveBeenCalledTimes(1);
    const msg = chromeMock.runtime.sendMessage.mock.calls[0][0];
    expect(msg.type).toBe('SAVE_TO_KNOWLEDGE');
    expect(msg.payload.content).toBe('右键选中文本');
    expect(msg.payload.title).toBe('页面标题');
    expect(msg.payload.source).toBe(location.href);
  });

  it('menuItemId 不匹配时不发送消息', () => {
    handleContextMenuClick({ selectionText: 'x', menuItemId: 'other-id' });
    expect(chromeMock.runtime.sendMessage).not.toHaveBeenCalled();
  });

  it('onMessage 监听器响应 EXTRACT_PAGE_CONTENT', async () => {
    const listeners = chromeMock.runtime.onMessage._listeners;
    expect(listeners.length).toBeGreaterThan(0);
    document.title = '监听测试页';
    const sendResponse = vi.fn();
    listeners[0]({ type: 'EXTRACT_PAGE_CONTENT' }, {}, sendResponse);
    await vi.waitFor(() => expect(sendResponse).toHaveBeenCalled());
    expect(sendResponse).toHaveBeenCalledWith({
      ok: true,
      data: expect.objectContaining({ title: '监听测试页' }),
    });
  });
});
