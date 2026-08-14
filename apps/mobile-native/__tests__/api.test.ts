// API 服务测试：mock 全局 fetch，验证各接口与 401 处理
import { chat, getBaseUrl, listAgents, listMemories, login } from '../src/services/api'
import { useAuthStore } from '../src/stores/authStore'

// mock 全局 fetch
global.fetch = jest.fn() as unknown as jest.Mock

beforeEach(() => {
  ;(global.fetch as jest.Mock).mockReset()
  useAuthStore.getState().logout()
})

describe('api 服务', () => {
  it('login 成功返回 token 与 user', async () => {
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ access_token: 'tok-1', user: { email: 'a@b.com' } }),
    })
    const result = await login('a@b.com', 'pass')
    expect(result.access_token).toBe('tok-1')
    expect(result.user.email).toBe('a@b.com')
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/auth/login'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'a@b.com', password: 'pass' }),
      }),
    )
  })

  it('login 失败抛出 ApiError 并携带消息', async () => {
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: false,
      status: 401,
      statusText: 'Unauthorized',
      json: async () => ({ detail: '凭据无效' }),
    })
    await expect(login('a@b.com', 'wrong')).rejects.toThrow('凭据无效')
  })

  it('listAgents 携带 Bearer token 并返回数组', async () => {
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ items: [{ id: '1', name: 'Bot' }] }),
    })
    const list = await listAgents('tok-1')
    expect(list).toHaveLength(1)
    expect(list[0].name).toBe('Bot')
    const callArgs = (global.fetch as jest.Mock).mock.calls[0]
    const init = callArgs[1]
    expect(init.headers.Authorization).toBe('Bearer tok-1')
  })

  it('listAgents 兼容裸数组返回结构', async () => {
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => [{ id: '2', name: 'Agent2' }],
    })
    const list = await listAgents('tok-1')
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe('2')
  })

  it('chat 发送消息并返回回复', async () => {
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ reply: '你好' }),
    })
    const result = await chat('tok-1', 'agent-1', 'hi')
    expect(result.reply).toBe('你好')
    const callArgs = (global.fetch as jest.Mock).mock.calls[0]
    const url = callArgs[0]
    const init = callArgs[1]
    expect(url).toContain('/api/v1/agents/agent-1/chat')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ message: 'hi' })
  })

  it('listMemories 返回记忆向量数组', async () => {
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ items: [{ id: 'm1', content: '记忆' }] }),
    })
    const list = await listMemories('tok-1')
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe('m1')
  })

  it('401 自动清除 token（登出）', async () => {
    useAuthStore.getState().login('tok-1', { email: 'a@b.com' })
    expect(useAuthStore.getState().isAuthenticated()).toBe(true)
    ;(global.fetch as jest.Mock).mockResolvedValue({
      ok: false,
      status: 401,
      statusText: 'Unauthorized',
      json: async () => ({ detail: 'token 过期' }),
    })
    await expect(listAgents('tok-1')).rejects.toThrow()
    expect(useAuthStore.getState().token).toBeNull()
    expect(useAuthStore.getState().isAuthenticated()).toBe(false)
  })

  it('getBaseUrl 默认指向本地 20200', () => {
    expect(getBaseUrl()).toBe('http://localhost:20200')
  })
})
