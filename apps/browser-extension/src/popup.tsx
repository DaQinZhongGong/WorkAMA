// WorkAMA Assistant 弹窗 UI
// 状态：
//  - 未登录：显示邮箱/密码登录表单
//  - 已登录：显示快速聊天输入框 + 最近对话列表

import { useEffect, useState } from 'react';
import type { ChatPayload, LoginPayload } from './messages';

type View = 'login' | 'chat';

interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
}

const TOKEN_KEY = 'workama_token';

export default function Popup() {
  const [view, setView] = useState<View>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [chatInput, setChatInput] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sending, setSending] = useState(false);

  // 挂载时检查是否已登录（chrome.storage.local 中存在 token）
  useEffect(() => {
    chrome.storage.local.get(TOKEN_KEY).then((res) => {
      if (res[TOKEN_KEY]) {
        setView('chat');
      }
    });
  }, []);

  async function handleLogin(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    const payload: LoginPayload = { email, password };
    const res = await chrome.runtime.sendMessage({ type: 'LOGIN', payload });
    if (res?.ok) {
      // background 已写入 token，这里同步刷新本地缓存并切换视图
      await chrome.storage.local.set({ [TOKEN_KEY]: res.data?.token ?? '' });
      setView('chat');
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
    const res = await chrome.runtime.sendMessage({ type: 'CHAT', payload });
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

  if (view === 'login') {
    return (
      <div className="popup">
        <h1>WorkAMA</h1>
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
      <h1>WorkAMA</h1>
      <ul className="messages" aria-label="消息列表">
        {messages.map((m, i) => (
          <li key={i} className={m.role}>{m.text}</li>
        ))}
      </ul>
      <input
        type="text"
        aria-label="聊天输入"
        placeholder="输入消息..."
        value={chatInput}
        onChange={(e) => setChatInput(e.target.value)}
      />
      <button type="button" onClick={handleSend} disabled={sending}>
        发送
      </button>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}
