// AgentsScreen 测试：渲染 / 列表加载 / 空状态 / 下拉刷新
import React from 'react'
import { fireEvent, render, waitFor } from '@testing-library/react-native'
import { AgentsScreen } from '../src/screens/AgentsScreen'
import * as api from '../src/services/api'
import { useAuthStore } from '../src/stores/authStore'

jest.mock('../src/services/api')

beforeEach(() => {
  useAuthStore.getState().logout()
  useAuthStore.getState().login('tok-1', { email: 'a@b.com' })
  ;(api.listAgents as jest.Mock).mockReset()
})

describe('AgentsScreen', () => {
  it('渲染页面标题与刷新按钮', async () => {
    ;(api.listAgents as jest.Mock).mockResolvedValue([])
    const { getByText, getByTestId } = render(<AgentsScreen />)
    expect(getByText('Agents')).toBeTruthy()
    expect(getByTestId('agents-refresh')).toBeTruthy()
    await waitFor(() => expect(api.listAgents).toHaveBeenCalled())
  })

  it('列表加载展示 Agent', async () => {
    ;(api.listAgents as jest.Mock).mockResolvedValue([
      { id: '1', name: '写作助手' },
      { id: '2', name: '代码助手' },
    ])
    const { getByText } = render(<AgentsScreen />)
    await waitFor(() => {
      expect(getByText('写作助手')).toBeTruthy()
      expect(getByText('代码助手')).toBeTruthy()
    })
  })

  it('空列表显示空状态', async () => {
    ;(api.listAgents as jest.Mock).mockResolvedValue([])
    const { getByTestId } = render(<AgentsScreen />)
    await waitFor(() => {
      expect(getByTestId('agents-empty')).toBeTruthy()
    })
  })

  it('点击刷新重新加载列表', async () => {
    ;(api.listAgents as jest.Mock).mockResolvedValueOnce([])
    const { getByTestId, getByText } = render(<AgentsScreen />)
    await waitFor(() => expect(getByTestId('agents-empty')).toBeTruthy())
    ;(api.listAgents as jest.Mock).mockResolvedValueOnce([{ id: '1', name: '新助手' }])
    fireEvent.press(getByTestId('agents-refresh'))
    await waitFor(() => {
      expect(getByText('新助手')).toBeTruthy()
    })
  })
})
