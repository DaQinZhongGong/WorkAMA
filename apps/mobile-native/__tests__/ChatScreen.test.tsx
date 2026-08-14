// ChatScreen 测试：渲染 / 空消息列表 / 发送消息 / 消息追加 / 输入框清空
import React from 'react'
import { fireEvent, render, waitFor } from '@testing-library/react-native'
import { ChatScreen } from '../src/screens/ChatScreen'
import * as api from '../src/services/api'
import { useAuthStore } from '../src/stores/authStore'

jest.mock('../src/services/api')

beforeEach(() => {
  useAuthStore.getState().logout()
  useAuthStore.getState().login('tok-1', { email: 'a@b.com' })
  ;(api.chat as jest.Mock).mockReset()
})

describe('ChatScreen', () => {
  it('渲染输入框与发送按钮', () => {
    const { getByTestId } = render(<ChatScreen agentId="a1" />)
    expect(getByTestId('chat-input')).toBeTruthy()
    expect(getByTestId('chat-send')).toBeTruthy()
  })

  it('初始空消息列表显示空状态', () => {
    const { getByTestId } = render(<ChatScreen agentId="a1" />)
    expect(getByTestId('chat-empty')).toBeTruthy()
  })

  it('发送消息调用 chat API', async () => {
    ;(api.chat as jest.Mock).mockResolvedValue({ reply: '你好' })
    const { getByTestId } = render(<ChatScreen agentId="a1" />)
    fireEvent.changeText(getByTestId('chat-input'), 'hello')
    fireEvent.press(getByTestId('chat-send'))
    await waitFor(() => {
      expect(api.chat).toHaveBeenCalledWith('tok-1', 'a1', 'hello')
    })
  })

  it('发送后用户消息追加到列表', async () => {
    ;(api.chat as jest.Mock).mockResolvedValue({ reply: '你好' })
    const { getByTestId, getByText } = render(<ChatScreen agentId="a1" />)
    fireEvent.changeText(getByTestId('chat-input'), 'hello')
    fireEvent.press(getByTestId('chat-send'))
    await waitFor(() => {
      expect(getByText('hello')).toBeTruthy()
    })
  })

  it('发送后输入框清空', async () => {
    ;(api.chat as jest.Mock).mockResolvedValue({ reply: '你好' })
    const { getByTestId } = render(<ChatScreen agentId="a1" />)
    fireEvent.changeText(getByTestId('chat-input'), 'hello')
    fireEvent.press(getByTestId('chat-send'))
    await waitFor(() => {
      expect(getByTestId('chat-input').props.value).toBe('')
    })
  })
})
