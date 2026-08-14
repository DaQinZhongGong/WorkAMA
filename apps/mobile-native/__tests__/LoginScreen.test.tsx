// LoginScreen 测试：渲染 / 输入 / 登录成功 / 登录失败 / loading / 微信按钮 / 错误清除
import React from 'react'
import { act, fireEvent, render, waitFor } from '@testing-library/react-native'
import { LoginScreen } from '../src/screens/LoginScreen'
import * as api from '../src/services/api'
import { useAuthStore } from '../src/stores/authStore'

jest.mock('../src/services/api')

beforeEach(() => {
  useAuthStore.getState().logout()
  ;(api.login as jest.Mock).mockReset()
})

describe('LoginScreen', () => {
  it('渲染登录页：标题、邮箱、密码、登录与微信按钮', () => {
    const { getByText, getByTestId } = render(<LoginScreen />)
    expect(getByText('WorkAMA')).toBeTruthy()
    expect(getByTestId('login-email')).toBeTruthy()
    expect(getByTestId('login-password')).toBeTruthy()
    expect(getByTestId('login-submit')).toBeTruthy()
    expect(getByTestId('login-wechat')).toBeTruthy()
  })

  it('输入邮箱与密码并提交', async () => {
    ;(api.login as jest.Mock).mockResolvedValue({ access_token: 't', user: { email: 'a@b.com' } })
    const { getByTestId } = render(<LoginScreen />)
    fireEvent.changeText(getByTestId('login-email'), 'a@b.com')
    fireEvent.changeText(getByTestId('login-password'), 'pass123')
    fireEvent.press(getByTestId('login-submit'))
    await waitFor(() => {
      expect(api.login).toHaveBeenCalledWith('a@b.com', 'pass123')
    })
  })

  it('登录成功后写入 authStore', async () => {
    ;(api.login as jest.Mock).mockResolvedValue({
      access_token: 'tok-x',
      user: { email: 'a@b.com', display_name: 'Alice' },
    })
    const { getByTestId } = render(<LoginScreen />)
    fireEvent.changeText(getByTestId('login-email'), 'a@b.com')
    fireEvent.changeText(getByTestId('login-password'), 'pass')
    fireEvent.press(getByTestId('login-submit'))
    await waitFor(() => {
      expect(useAuthStore.getState().token).toBe('tok-x')
      expect(useAuthStore.getState().user?.display_name).toBe('Alice')
    })
  })

  it('登录失败显示错误信息', async () => {
    ;(api.login as jest.Mock).mockRejectedValue(new Error('凭据无效'))
    const { getByTestId, getByText } = render(<LoginScreen />)
    fireEvent.changeText(getByTestId('login-email'), 'a@b.com')
    fireEvent.changeText(getByTestId('login-password'), 'wrong')
    fireEvent.press(getByTestId('login-submit'))
    await waitFor(() => {
      expect(getByText('凭据无效')).toBeTruthy()
    })
  })

  it('登录中显示 loading 状态并禁用按钮', async () => {
    // 用可控 Promise 模拟登录中
    let resolveLogin: (value: unknown) => void = () => {}
    ;(api.login as jest.Mock).mockReturnValue(
      new Promise((resolve) => {
        resolveLogin = resolve
      }),
    )
    const { getByTestId, getByText } = render(<LoginScreen />)
    fireEvent.changeText(getByTestId('login-email'), 'a@b.com')
    fireEvent.changeText(getByTestId('login-password'), 'pass')
    fireEvent.press(getByTestId('login-submit'))
    await waitFor(() => {
      expect(getByText('登录中...')).toBeTruthy()
    })
    // loading 期间按钮禁用：disabled 或 accessibilityState.disabled 任一为真
    const submit = getByTestId('login-submit')
    const isDisabled =
      submit.props.disabled === true ||
      submit.props.accessibilityState?.disabled === true
    expect(isDisabled).toBe(true)
    // 释放 Promise，并用 act 包裹随之触发的状态更新，避免警告
    await act(async () => {
      resolveLogin({ access_token: 't', user: { email: 'a@b.com' } })
    })
  })

  it('微信登录按钮存在且可按压', () => {
    const { getByTestId } = render(<LoginScreen />)
    const wechat = getByTestId('login-wechat')
    expect(wechat).toBeTruthy()
    // 按压不抛错即可（Alert 在测试环境中不弹窗）
    fireEvent.press(wechat)
  })

  it('错误在重新输入时清除', async () => {
    ;(api.login as jest.Mock).mockRejectedValueOnce(new Error('凭据无效'))
    const { getByTestId, queryByText } = render(<LoginScreen />)
    fireEvent.changeText(getByTestId('login-email'), 'a@b.com')
    fireEvent.changeText(getByTestId('login-password'), 'wrong')
    fireEvent.press(getByTestId('login-submit'))
    await waitFor(() => expect(queryByText('凭据无效')).toBeTruthy())
    // 重新输入邮箱应清除错误
    fireEvent.changeText(getByTestId('login-email'), 'b@c.com')
    await waitFor(() => expect(queryByText('凭据无效')).toBeNull())
  })
})
