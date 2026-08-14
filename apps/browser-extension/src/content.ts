// WorkAMA Assistant 内容脚本
// 注入到所有页面，负责：
//  1. 提取页面内容（标题 / 正文 / 选中文本）
//  2. 监听右键菜单点击，将选中内容发送到后台保存到知识库
//  3. 响应 background 的 EXTRACT_PAGE_CONTENT 消息

import type { PageContent, WorkamaMessage, SaveToKnowledgePayload } from './messages';

/**
 * 提取当前页面的标题、正文与选中文本。
 * 优先返回选中文本；未选中时返回正文摘要。
 * @param doc 可选，便于单元测试传入 mock document
 */
export function extractPageContent(doc: Document = document): PageContent {
  const selection = (doc.getSelection?.() ?? (typeof window !== 'undefined' ? window.getSelection?.() : null) ?? '')
    .toString()
    .trim();
  const title = doc.title || '';
  const bodyText = doc.body?.innerText ?? '';
  return {
    url: typeof location !== 'undefined' ? location.href : '',
    title,
    text: selection || bodyText,
    selection,
  };
}

/**
 * 处理右键菜单点击事件。
 * 优先使用 contextMenus 传入的 selectionText，回退到当前页面选区。
 * 构造 SAVE_TO_KNOWLEDGE 消息发送给 background。
 */
export function handleContextMenuClick(
  info: { selectionText?: string; menuItemId?: string },
  _tab?: unknown,
): void {
  if (info.menuItemId !== 'send-to-workama') {
    return;
  }
  const content = extractPageContent();
  const selection = info.selectionText ?? content.selection;
  const payload: SaveToKnowledgePayload = {
    title: content.title,
    content: selection || content.text,
    source: content.url,
  };
  const message: WorkamaMessage<SaveToKnowledgePayload> = {
    type: 'SAVE_TO_KNOWLEDGE',
    payload,
  };
  chrome.runtime.sendMessage(message);
}

// ---- 顶层事件注册 ----

chrome.runtime.onMessage.addListener((message: WorkamaMessage, _sender, sendResponse) => {
  if (message?.type === 'EXTRACT_PAGE_CONTENT') {
    sendResponse({ ok: true, data: extractPageContent() });
  }
  return true;
});

chrome.contextMenus?.onClicked?.addListener(
  handleContextMenuClick as unknown as (
    info: { selectionText?: string; menuItemId?: string },
    tab?: unknown,
  ) => void,
);
