import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ShareApp } from './ShareApp'

type WriteTextMock = ReturnType<typeof vi.fn>

function renderAt(route: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ShareApp />
    </MemoryRouter>,
  )
}

describe('ShareApp', () => {
  let writeText: WriteTextMock

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined)
    // ShareApp reads navigator.clipboard via optional chaining; provide a stub.
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
      writable: true,
    })
    // Fake timers keep the 1800ms reset from leaking across tests.
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('renders the landing view on a non-artifact route without crashing', () => {
    renderAt('/')
    expect(screen.getByText('Share accountable work with context.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /open demo artifact/i })).toBeInTheDocument()
  })

  it('renders the artifact view on an /artifact/ route without crashing', () => {
    renderAt('/artifact/demo')
    expect(screen.getByRole('heading', { name: 'Approval flow: launch readiness' })).toBeInTheDocument()
    expect(screen.getByText('SHARED ARTIFACT')).toBeInTheDocument()
  })

  it('shows the landing-page title, lead, feature grid, and navigation links', () => {
    renderAt('/')
    expect(screen.getByText('WORKAMA PLATFORM')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Publish a scoped artifact or an approved application view without exposing the workspace behind it.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('Evidence stays attached')).toBeInTheDocument()
    expect(screen.getByText('Scope is explicit')).toBeInTheDocument()
    expect(screen.getByText('Built for teams')).toBeInTheDocument()
    const demo = screen.getByRole('link', { name: /open demo artifact/i })
    expect(demo).toHaveAttribute('href', '/artifact/demo')
    expect(screen.getByRole('link', { name: /read developer docs/i })).toBeInTheDocument()
  })

  it('shows the artifact title, lead, meta, decision brief, and policy callout', () => {
    renderAt('/artifact/launch-readiness')
    expect(
      screen.getByText('A governed work artifact shared from the Product workspace.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Verified provenance')).toBeInTheDocument()
    expect(screen.getByText('Updated today')).toBeInTheDocument()
    expect(screen.getByText('Read-only')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Decision brief' })).toBeInTheDocument()
    expect(screen.getByText('Policy checked')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Share scope is limited to this artifact. Workspace credentials are never included.',
      ),
    ).toBeInTheDocument()
  })

  it('keeps landing and artifact content isolated to their routes', () => {
    const { unmount: unmountLanding } = renderAt('/')
    expect(screen.queryByText('Approval flow: launch readiness')).not.toBeInTheDocument()
    expect(screen.queryByText('Copy share link')).not.toBeInTheDocument()
    unmountLanding()

    renderAt('/artifact/demo')
    expect(screen.queryByText('Share accountable work with context.')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /open demo artifact/i })).not.toBeInTheDocument()
  })

  it('writes the current URL to the clipboard when the copy button is clicked', async () => {
    renderAt('/artifact/demo')
    const expectedHref = window.location.href
    const button = screen.getByRole('button', { name: /copy share link/i })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(expectedHref)
  })

  it('toggles the button label to "Link copied" and reverts after the timeout', async () => {
    renderAt('/artifact/demo')
    const button = screen.getByRole('button', { name: /copy share link/i })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(screen.getByRole('button', { name: /link copied/i })).toBeInTheDocument()
    await act(async () => {
      vi.advanceTimersByTime(1800)
    })
    expect(screen.getByRole('button', { name: /copy share link/i })).toBeInTheDocument()
  })

  it('still marks the link as copied when the clipboard API is unavailable', async () => {
    // Simulate a host where navigator.clipboard is absent (component uses optional chaining).
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
      writable: true,
    })
    renderAt('/artifact/demo')
    const button = screen.getByRole('button', { name: /copy share link/i })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(screen.getByRole('button', { name: /link copied/i })).toBeInTheDocument()
    expect(writeText).not.toHaveBeenCalled()
  })

  it('renders the static artifact view identically for text, code, and json artifact routes', () => {
    for (const route of ['/artifact/text', '/artifact/code', '/artifact/json']) {
      const { unmount } = renderAt(route)
      expect(screen.getByRole('heading', { name: 'Approval flow: launch readiness' })).toBeInTheDocument()
      expect(screen.getByText('SHARED ARTIFACT')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /copy share link/i })).toBeInTheDocument()
      unmount()
    }
  })
})
