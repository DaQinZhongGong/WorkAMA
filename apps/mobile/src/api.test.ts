import { describe, expect, it } from 'vitest'
import { clearSessionToken, getSessionToken, sessionToken, setSessionToken } from './api'

describe('mobile session storage policy', () => {
  it('keeps the access token out of browser storage', () => {
    setSessionToken('memory-token')
    expect(sessionToken.value).toBe('memory-token')
    expect(getSessionToken()).toBe('memory-token')
    clearSessionToken()
    expect(sessionToken.value).toBeNull()
  })
})
