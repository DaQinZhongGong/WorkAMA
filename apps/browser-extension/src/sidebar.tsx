// WorkAMA Assistant 侧边栏 UI
// 在 Chrome 侧边栏中常驻显示，提供 未登录 / 聊天 / 保存到知识库 三视图切换。
// 复用 popup 的登录与聊天交互模式，并扩展知识库保存能力。

import { useEffect, useState } from 'react';
import type {
  ApiResponse,
  ChatPayload,
  ChatResponse,
  LoginPayload,
  LoginResponse,
  PageContent,
  SaveToKnowledgePayload,
} from './messages';

type View = 'login' | 'chat' | 'save';

interface SidebarMessage {
  role: 'user' | 'assistant';
  text: string;
}

const TOKEN_KEY = 'workama_token';
const WELCOME_TEXT = 'WorkAMA 已就绪，输入消息开始对话。';

export default function Sidebar() {
  const [view, setView] = useState<View>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [chatInput, setChatInput] = useState('');
  const [messages, setMessages] = useState<SidebarMessage[]>([]);
  const [sending, setSending] = useState(false);

  // 保存视图状态
  const [pageTitle, setPageTitle] = useState('');
  const [pageUrl, setPageUrl] = useState('');
  const [pageSelection, setPageSelection] = useState('');
  const [knowledgeTitle, setKnowledgeTitle] = useState('');
  const [knowledgeContent, setKnowledgeContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedTip, setSavedTip] = useState('');

  // 挂载时检查是否已登录（chrome.storage.local 中存在 token）
  useEffect(() => {
    chrome.storage.local.get(TOKEN_KEY).then((res) => {
      if (res[TOKEN_KEY]) {
        setView('chat');
        setMessages([{ role: 'assistant', text: WELCOME_TEXT }]);
      }
    });
  }, []);

  // 进入保存视图时自动通过 background 获取当前页面内容
  useEffect(() => {
    if (view !== 'save') {
      return;
    }
    void chrome.runtime
      .sendMessage({ type: 'EXTRACT_PAGE_CONTENT' })
      .then((raw: unknown) => {
        const res = raw as ApiResponse<PageContent> | undefined;
        if (res?.ok && res.data) {
          const page = res.data;
          setPageTitle(page.title ?? '');
          setPageUrl(page.url ?? '');
          setPageSelection(page.selection ?? '');
          setKnowledgeTitle(page.title ?? '');
          setKnowledgeContent(page.selection || page.text || '');
        }
      });
  }, [view]);

  async function handleLogin(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    const payload: LoginPayload = { email, password };
    const res = (await chrome.runtime.sendMessage({
      type: 'LOGIN',
      payload,
    })) as ApiResponse<LoginResponse>;
    if (res?.ok) {
      // background 已写入 token，这里同步刷新本地缓存并切换视图
      await chrome.storage.local.set({ [TOKEN_KEY]: res.data?.token ?? '' });
      setView('chat');
      setMessages([{ role: 'assistant', text: WELCOME_TEXT }]);
    } else {
      setError(res?.error ?? '登录失败');
    }
  }

  async function handleSend() {
    const trimmed = chatInput.trim();
    if (!trimmed || sending) {
      return;
    }
    setSending(true);
    setMessages((prev) => [...prev, { role: 'user', text: trimmed }]);
    setChatInput('');
    const payload: ChatPayload = { message: trimmed };
    const res = (await chrome.runtime.sendMessage({
      type: 'CHAT',
      payload,
    })) as ApiResponse<ChatResponse>;
    setSending(false);
    if (res?.ok) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: res.data?.reply ?? '' },
      ]);
    } else {
      setError(res?.error ?? '发送失败');
    }
  }

  async function handleSave() {
    const title = knowledgeTitle.trim();
    const content = knowledgeContent.trim();
    if (!title || !content || saving) {
      return;
    }
    setSaving(true);
    setSavedTip('');
    const payload: SaveToKnowledgePayload = { title, content, source: pageUrl };
    const res = (await chrome.runtime.sendMessage({
      type: 'SAVE_TO_KNOWLEDGE',
      payload,
    })) as ApiResponse<unknown>;
    setSaving(false);
    if (res?.ok) {
      setSavedTip('已保存');
    } else {
      setError(res?.error ?? '保存失败');
    }
  }

  async function handleLogout() {
    // 清除 token 并重置状态，回到登录视图
    await chrome.storage.local.set({ [TOKEN_KEY]: '' });
    setEmail('');
    setPassword('');
    setChatInput('');
    setMessages([]);
    setError('');
    setSavedTip('');
    setView('login');
  }

  function switchView(next: 'chat' | 'save') {
    setView(next);
    setError('');
    setSavedTip('');
  }

  // 未登录视图：仅显示登录表单
  if (view === 'login') {
    return (
      <div className="popup">
        <h1>WorkAMA 侧边栏</h1>
        <form onSubmit={handleLogin}>
          <input
            type="email"
            aria-label="邮箱"
            placeholder="邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <input
            type="password"
            aria-label="密码"
            placeholder="密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button type="submit">登录</button>
        </form>
        {error ? <p className="error" role="alert">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className="popup">
      <h1>WorkAMA 侧边栏</h1>
      <div role="tablist">
        <button
          role="tab"
          aria-selected={view === 'chat'}
          onClick={() => switchView('chat')}
        >
          聊天
        </button>
        <button
          role="tab"
          aria-selected={view === 'save'}
          onClick={() => switchView('save')}
        >
          保存
        </button>
        <button
          role="tab"
          aria-selected={false}
          onClick={() => void handleLogout()}
        >
          退出登录
        </button>
      </div>

      {view === 'chat' ? (
        <>
          <ul className="messages" aria-label="侧边栏消息列表">
            {messages.map((m, i) => (
              <li key={i} className={m.role}>{m.text}</li>
            ))}
          </ul>
          <textarea
            aria-label="侧边栏聊天输入"
            placeholder="输入消息..."
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={(e) => {
              // Enter 发送，Shift+Enter 换行
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void handleSend();
              }
            }}
          />
          <button type="button" onClick={handleSend} disabled={sending}>
            发送
          </button>
          {error ? <p className="error" role="alert">{error}</p> : null}
        </>
      ) : (
        <>
          <div className="page-info">
            <p>页面标题：{pageTitle}</p>
            <p>页面 URL：{pageUrl}</p>
            <p>选中文本：{pageSelection}</p>
          </div>
          <input
            type="text"
            aria-label="知识库标题"
            placeholder="标题"
            value={knowledgeTitle}
            onChange={(e) => setKnowledgeTitle(e.target.value)}
          />
          <textarea
            aria-label="知识库内容"
            placeholder="内容"
            value={knowledgeContent}
            onChange={(e) => setKnowledgeContent(e.target.value)}
          />
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
          >
            保存到知识库
          </button>
          {savedTip ? <p role="status">{savedTip}</p> : null}
          {error ? <p className="error" role="alert">{error}</p> : null}
        </>
      )}
    </div>
  );
}
