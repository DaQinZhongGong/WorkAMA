// sidebar.tsx 单元测试
// 覆盖：登录表单渲染 / 已登录直接进聊天并显示欢迎语 / 登录成功 / 登录失败 /
//       聊天输入更新 / 发送按钮触发 CHAT / 切换保存视图触发 EXTRACT_PAGE_CONTENT /
//       保存按钮触发 SAVE_TO_KNOWLEDGE / 退出登录清除 token 并回到登录视图

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import Sidebar from '../src/sidebar';

const chromeMock = (globalThis as unknown as { __chromeMock: any }).__chromeMock;

describe('Sidebar UI', () => {
  beforeEach(() => {
    chromeMock.runtime.sendMessage.mockClear();
  });

  it('未登录时渲染登录表单（邮箱、密码、登录按钮）', () => {
    render(<Sidebar />);
    expect(screen.getByLabelText('邮箱')).toBeInTheDocument();
    expect(screen.getByLabelText('密码')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录' })).toBeInTheDocument();
  });

  it('已存在 token 时默认进入聊天视图并显示欢迎语', async () => {
    chromeMock.storage.local._data.workama_token = 'existing-tok';
    render(<Sidebar />);
    await waitFor(() =>
      expect(screen.getByLabelText('侧边栏聊天输入')).toBeInTheDocument(),
    );
    expect(
      screen.getByText('WorkAMA 已就绪，输入消息开始对话。'),
    ).toBeInTheDocument();
  });

  it('登录成功后切换到聊天视图并保存 token', async () => {
    chromeMock.runtime.sendMessage.mockResolvedValue({
      ok: true,
      data: { token: 'tok-success' },
    });
    render(<Sidebar />);
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: 'a@b.com' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'pass' },
    });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() =>
      expect(screen.getByLabelText('侧边栏聊天输入')).toBeInTheDocument(),
    );
    expect(chromeMock.runtime.sendMessage).toHaveBeenCalledWith({
      type: 'LOGIN',
      payload: { email: 'a@b.com', password: 'pass' },
    });
    expect(chromeMock.storage.local._data.workama_token).toBe('tok-success');
  });

  it('登录失败时显示错误信息', async () => {
    chromeMock.runtime.sendMessage.mockResolvedValue({
      ok: false,
      error: '凭据无效',
    });
    render(<Sidebar />);
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: 'a@b.com' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'bad' },
    });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('凭据无效'),
    );
  });

  it('在聊天输入框中输入会更新其值', async () => {
    chromeMock.storage.local._data.workama_token = 'tok';
    render(<Sidebar />);
    const input = await screen.findByLabelText('侧边栏聊天输入');
    fireEvent.change(input, { target: { value: '你好' } });
    expect((input as HTMLTextAreaElement).value).toBe('你好');
  });

  it('点击发送按钮触发 CHAT 消息', async () => {
    chromeMock.runtime.sendMessage.mockImplementation((msg: { type: string }) => {
      if (msg.type === 'CHAT') {
        return Promise.resolve({ ok: true, data: { reply: '收到' } });
      }
      return Promise.resolve({ ok: true, data: { token: 'tok' } });
    });
    render(<Sidebar />);
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: 'a@b.com' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'p' },
    });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    const input = await screen.findByLabelText('侧边栏聊天输入');
    fireEvent.change(input, { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() =>
      expect(chromeMock.runtime.sendMessage).toHaveBeenCalledWith({
        type: 'CHAT',
        payload: { message: '你好' },
      }),
    );
  });

  it('切换到保存视图时触发 EXTRACT_PAGE_CONTENT 获取页面内容', async () => {
    chromeMock.runtime.sendMessage.mockImplementation((msg: { type: string }) => {
      if (msg.type === 'EXTRACT_PAGE_CONTENT') {
        return Promise.resolve({
          ok: true,
          data: {
            url: 'https://x.com',
            title: 'X页',
            text: '正文',
            selection: '选中',
          },
        });
      }
      return Promise.resolve({ ok: true, data: { token: 'tok' } });
    });
    render(<Sidebar />);
    // 先登录进入聊天视图
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: 'a@b.com' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'p' },
    });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() =>
      expect(screen.getByLabelText('侧边栏聊天输入')).toBeInTheDocument(),
    );
    // 切换到保存视图
    fireEvent.click(screen.getByRole('tab', { name: '保存' }));
    await waitFor(() =>
      expect(chromeMock.runtime.sendMessage).toHaveBeenCalledWith({
        type: 'EXTRACT_PAGE_CONTENT',
      }),
    );
    // 验证页面内容已展示
    expect(await screen.findByText(/X页/)).toBeInTheDocument();
    expect(screen.getByText(/https:\/\/x\.com/)).toBeInTheDocument();
    expect(screen.getByText(/选中/)).toBeInTheDocument();
  });

  it('点击保存按钮触发 SAVE_TO_KNOWLEDGE 并显示已保存', async () => {
    chromeMock.runtime.sendMessage.mockImplementation((msg: { type: string }) => {
      if (msg.type === 'EXTRACT_PAGE_CONTENT') {
        return Promise.resolve({
          ok: true,
          data: {
            url: 'https://x.com',
            title: 'X页',
            text: '正文',
            selection: '选中',
          },
        });
      }
      if (msg.type === 'SAVE_TO_KNOWLEDGE') {
        return Promise.resolve({ ok: true, data: { id: 'k1' } });
      }
      return Promise.resolve({ ok: true, data: { token: 'tok' } });
    });
    render(<Sidebar />);
    // 先登录
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: 'a@b.com' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'p' },
    });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() =>
      expect(screen.getByLabelText('侧边栏聊天输入')).toBeInTheDocument(),
    );
    // 切换到保存视图并等待页面内容加载
    fireEvent.click(screen.getByRole('tab', { name: '保存' }));
    await screen.findByText(/X页/);
    // 点击保存按钮
    fireEvent.click(screen.getByRole('button', { name: '保存到知识库' }));
    await waitFor(() =>
      expect(chromeMock.runtime.sendMessage).toHaveBeenCalledWith({
        type: 'SAVE_TO_KNOWLEDGE',
        payload: {
          title: 'X页',
          content: '选中',
          source: 'https://x.com',
        },
      }),
    );
    expect(screen.getByText('已保存')).toBeInTheDocument();
  });

  it('点击退出登录清除 token 并回到登录视图', async () => {
    chromeMock.runtime.sendMessage.mockResolvedValue({
      ok: true,
      data: { token: 'tok' },
    });
    render(<Sidebar />);
    // 先登录
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: 'a@b.com' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'p' },
    });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() =>
      expect(screen.getByLabelText('侧边栏聊天输入')).toBeInTheDocument(),
    );
    expect(chromeMock.storage.local._data.workama_token).toBe('tok');
    // 点击退出登录 tab
    fireEvent.click(screen.getByRole('tab', { name: '退出登录' }));
    await waitFor(() =>
      expect(screen.getByLabelText('邮箱')).toBeInTheDocument(),
    );
    // token 已清除
    expect(chromeMock.storage.local._data.workama_token).toBe('');
    // 顶部 tab 已不可见
    expect(
      screen.queryByRole('tab', { name: '聊天' }),
    ).not.toBeInTheDocument();
  });
});
