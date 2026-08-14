// authStore 测试：初始状态 / login / logout / isAuthenticated
import { useAuthStore } from '../src/stores/authStore'

beforeEach(() => {
  useAuthStore.getState().logout()
})

describe('authStore', () => {
  it('初始状态：token 与 user 为空，未认证', () => {
    expect(useAuthStore.getState().token).toBeNull()
    expect(useAuthStore.getState().user).toBeNull()
    expect(useAuthStore.getState().isAuthenticated()).toBe(false)
  })

  it('login 设置 token', () => {
    useAuthStore.getState().login('tok-1', { email: 'a@b.com' })
    expect(useAuthStore.getState().token).toBe('tok-1')
  })

  it('login 设置 user', () => {
    useAuthStore.getState().login('tok-1', { email: 'a@b.com', display_name: 'Alice' })
    expect(useAuthStore.getState().user?.email).toBe('a@b.com')
    expect(useAuthStore.getState().user?.display_name).toBe('Alice')
  })

  it('logout 清除 token 与 user', () => {
    useAuthStore.getState().login('tok-1', { email: 'a@b.com' })
    useAuthStore.getState().logout()
    expect(useAuthStore.getState().token).toBeNull()
    expect(useAuthStore.getState().user).toBeNull()
  })

  it('isAuthenticated 在登录后为 true，登出后为 false', () => {
    expect(useAuthStore.getState().isAuthenticated()).toBe(false)
    useAuthStore.getState().login('tok-1', { email: 'a@b.com' })
    expect(useAuthStore.getState().isAuthenticated()).toBe(true)
    useAuthStore.getState().logout()
    expect(useAuthStore.getState().isAuthenticated()).toBe(false)
  })
})
