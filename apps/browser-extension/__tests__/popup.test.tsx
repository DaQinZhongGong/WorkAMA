// popup.tsx 单元测试
// 覆盖：登录表单渲染 / 已登录直接进聊天 / 登录成功 / 登录失败 /
//       聊天输入更新 / 发送按钮触发 CHAT

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import Popup from '../src/popup';

const chromeMock = (globalThis as unknown as { __chromeMock: any }).__chromeMock;

describe('Popup UI', () => {
  beforeEach(() => {
    chromeMock.runtime.sendMessage.mockClear();
  });

  it('未登录时渲染登录表单（邮箱、密码、登录按钮）', () => {
    render(<Popup />);
    expect(screen.getByLabelText('邮箱')).toBeInTheDocument();
    expect(screen.getByLabelText('密码')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录' })).toBeInTheDocument();
  });

  it('已存在 token 时直接显示聊天界面', async () => {
    chromeMock.storage.local._data.workama_token = 'existing-tok';
    render(<Popup />);
    await waitFor(() => expect(screen.getByLabelText('聊天输入')).toBeInTheDocument());
  });

  it('登录成功后切换到聊天界面并保存 token', async () => {
    chromeMock.runtime.sendMessage.mockResolvedValue({
      ok: true,
      data: { token: 'tok-success' },
    });
    render(<Popup />);
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'pass' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() => expect(screen.getByLabelText('聊天输入')).toBeInTheDocument());
    expect(chromeMock.runtime.sendMessage).toHaveBeenCalledWith({
      type: 'LOGIN',
      payload: { email: 'a@b.com', password: 'pass' },
    });
    expect(chromeMock.storage.local._data.workama_token).toBe('tok-success');
  });

  it('登录失败时显示错误信息', async () => {
    chromeMock.runtime.sendMessage.mockResolvedValue({ ok: false, error: '凭据无效' });
    render(<Popup />);
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'bad' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('凭据无效'));
  });

  it('在聊天输入框中输入会更新其值', async () => {
    chromeMock.storage.local._data.workama_token = 'tok';
    render(<Popup />);
    const input = await screen.findByLabelText('聊天输入');
    fireEvent.change(input, { target: { value: '你好' } });
    expect((input as HTMLInputElement).value).toBe('你好');
  });

  it('点击发送按钮触发 CHAT 消息', async () => {
    chromeMock.runtime.sendMessage.mockImplementation((msg: { type: string }) => {
      if (msg.type === 'CHAT') {
        return Promise.resolve({ ok: true, data: { reply: '收到' } });
      }
      return Promise.resolve({ ok: true, data: { token: 'tok' } });
    });
    render(<Popup />);
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'p' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    const input = await screen.findByLabelText('聊天输入');
    fireEvent.change(input, { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() =>
      expect(chromeMock.runtime.sendMessage).toHaveBeenCalledWith({
        type: 'CHAT',
        payload: { message: '你好' },
      }),
    );
  });
});
