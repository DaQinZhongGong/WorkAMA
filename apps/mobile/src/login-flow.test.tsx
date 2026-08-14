import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { type FormEvent, useState } from 'react'

/**
 * 移动端登录流程单元测试（P2 阶段）。
 *
 * 移动端 App.tsx 中的 LoginScreen 未导出且不含「记住我」/密码可见性切换，
 * 因此按任务要求在此文件内定义自包含 LoginForm 测试组件，覆盖完整登录交互契约。
 * 该组件模拟实际 App.tsx 的登录逻辑（POST /api/v1/auth/login + memory-only token）。
 */

// ============================================================================
// 测试用 LoginForm 组件（模拟 App.tsx 的登录契约）
// ============================================================================

type User = { display_name: string; email: string; role: string }
type LoginResponse = { access_token: string; user: User } | { mfa_required: true; mfa_ticket: string }

interface LoginFormProps {
  onSuccess?: (user: User, token: string) => void
  apiPost?: (url: string, body: Record<string, unknown>) => Promise<LoginResponse>
}

function validateEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
}

function LoginForm({ onSuccess, apiPost }: LoginFormProps) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError('')

    // 空字段验证
    if (!email.trim() || !password.trim()) {
      setError('请输入邮箱和密码')
      return
    }
    // 邮箱格式验证
    if (!validateEmail(email)) {
      setError('邮箱格式不正确')
      return
    }
    // 密码长度验证
    if (password.length < 6) {
      setError('密码长度至少 6 位')
      return
    }

    setBusy(true)
    try {
      const post = apiPost || defaultPost
      const result = await post('/api/v1/auth/login', { email, password })
      if ('mfa_required' in result) {
        setError('需要 MFA 验证')
        return
      }
      onSuccess?.(result.user, result.access_token)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '登录失败'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} data-testid="login-form">
      <label htmlFor="test-email">Email</label>
      <input
        id="test-email"
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="email@example.com"
        autoComplete="username"
      />

      <label htmlFor="test-password">Password</label>
      <div style={{ position: 'relative' }}>
        <input
          id="test-password"
          type={showPassword ? 'text' : 'password'}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password"
          autoComplete="current-password"
        />
        <button
          type="button"
          onClick={() => setShowPassword((v) => !v)}
          aria-label={showPassword ? 'hide password' : 'show password'}
          data-testid="password-toggle"
        >
          {showPassword ? '🙈' : '👁'}
        </button>
      </div>

      <label>
        <input
          type="checkbox"
          checked={remember}
          onChange={(e) => setRemember(e.target.checked)}
          data-testid="remember-me"
        />
        Remember me
      </label>

      {error && (
        <p className="notice error" role="alert" data-testid="login-error">
          {error}
        </p>
      )}

      <button type="submit" disabled={busy} data-testid="login-submit">
        {busy ? 'Signing in...' : 'Enter workspace'}
      </button>
    </form>
  )
}

// 默认 fetch 实现（可被测试覆盖）
async function defaultPost(url: string, body: Record<string, unknown>): Promise<LoginResponse> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (resp.status === 401) throw new Error('Invalid credentials')
  if (resp.status === 403) throw new Error('Account locked')
  if (!resp.ok) throw new Error(`Request failed (${resp.status})`)
  return resp.json()
}

// ============================================================================
// 测试
// ============================================================================

const mockUser: User = { display_name: 'Test User', email: 'test@example.com', role: 'owner' }
const mockResponse: LoginResponse = { access_token: 'mock-token-123', user: mockUser }

describe('LoginForm component', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders email input, password input, and submit button', () => {
    render(<LoginForm />)
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByTestId('login-submit')).toBeInTheDocument()
    expect(screen.getByText('Enter workspace')).toBeInTheDocument()
  })

  it('validates empty fields and shows error', async () => {
    render(<LoginForm />)
    fireEvent.click(screen.getByTestId('login-submit'))

    expect(await screen.findByTestId('login-error')).toHaveTextContent('请输入邮箱和密码')
  })

  it('validates email format', async () => {
    render(<LoginForm />)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'not-an-email' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password123' } })
    fireEvent.submit(screen.getByTestId('login-form'))

    expect(await screen.findByTestId('login-error')).toHaveTextContent('邮箱格式不正确')
  })

  it('validates password length (minimum 6 characters)', async () => {
    render(<LoginForm />)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: '12345' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    expect(await screen.findByTestId('login-error')).toHaveTextContent('密码长度至少 6 位')
  })

  it('calls API on submit with correct credentials', async () => {
    const apiPost = vi.fn().mockResolvedValue(mockResponse)
    render(<LoginForm apiPost={apiPost} />)

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/api/v1/auth/login', {
        email: 'test@example.com',
        password: 'password123',
      })
    })
  })

  it('shows loading state during submission', async () => {
    let resolveFn: (value: LoginResponse) => void = () => {}
    const apiPost = vi.fn().mockImplementation(() => new Promise<LoginResponse>((resolve) => { resolveFn = resolve }))

    render(<LoginForm apiPost={apiPost} />)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('login-submit')).toBeDisabled()
      expect(screen.getByText('Signing in...')).toBeInTheDocument()
    })

    resolveFn(mockResponse)
    await waitFor(() => {
      expect(screen.getByText('Enter workspace')).toBeInTheDocument()
    })
  })

  it('handles 401 error and displays message', async () => {
    const apiPost = vi.fn().mockRejectedValue(new Error('Invalid credentials'))
    render(<LoginForm apiPost={apiPost} />)

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrongpass' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    expect(await screen.findByTestId('login-error')).toHaveTextContent('Invalid credentials')
  })

  it('handles 403 error and displays message', async () => {
    const apiPost = vi.fn().mockRejectedValue(new Error('Account locked'))
    render(<LoginForm apiPost={apiPost} />)

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    expect(await screen.findByTestId('login-error')).toHaveTextContent('Account locked')
  })

  it('handles network error and displays fallback message', async () => {
    const apiPost = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    render(<LoginForm apiPost={apiPost} />)

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    expect(await screen.findByTestId('login-error')).toHaveTextContent('Failed to fetch')
  })

  it('calls onSuccess callback after successful login', async () => {
    const onSuccess = vi.fn()
    const apiPost = vi.fn().mockResolvedValue(mockResponse)
    render(<LoginForm apiPost={apiPost} onSuccess={onSuccess} />)

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'test@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByTestId('login-submit'))

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith(mockUser, 'mock-token-123')
    })
  })

  it('toggles remember me checkbox', () => {
    render(<LoginForm />)
    const checkbox = screen.getByTestId('remember-me') as HTMLInputElement

    expect(checkbox.checked).toBe(false)
    fireEvent.click(checkbox)
    expect(checkbox.checked).toBe(true)
    fireEvent.click(checkbox)
    expect(checkbox.checked).toBe(false)
  })

  it('toggles password visibility', () => {
    render(<LoginForm />)
    const passwordInput = screen.getByLabelText('Password') as HTMLInputElement
    const toggleButton = screen.getByTestId('password-toggle')

    expect(passwordInput.type).toBe('password')
    fireEvent.click(toggleButton)
    expect(passwordInput.type).toBe('text')
    fireEvent.click(toggleButton)
    expect(passwordInput.type).toBe('password')
  })
})
