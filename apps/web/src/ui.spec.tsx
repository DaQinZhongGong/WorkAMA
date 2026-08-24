import { afterEach, describe, expect, it, vi } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { LocaleProvider } from './locale'
import { Badge, Button, DataTable, EmptyAction, Field, IconButton, Kpi, Modal, Panel, SearchBox, StateView, Status, Toast } from './ui'

// vitest 未启用 globals，testing-library 的自动 cleanup 不会注册，
// 此处显式在每个用例后卸载 DOM，避免跨用例累积导致查询命中多个元素。
afterEach(() => cleanup())

function renderWithLocale(element: ReactElement) {
  return renderToStaticMarkup(<LocaleProvider initialLocale="en-US">{element}</LocaleProvider>)
}

// 用 LocaleProvider（部分组件依赖 useLocale）+ 可选 MemoryRouter（EmptyAction 依赖 Link）包裹后渲染到 jsdom，
// 配合 screen / fireEvent 进行真正的交互断言。
function renderWithProviders(ui: ReactElement, options: { router?: boolean } = {}) {
  const tree = options.router ? <MemoryRouter>{ui}</MemoryRouter> : ui
  return render(<LocaleProvider initialLocale="en-US">{tree}</LocaleProvider>)
}

describe('console UI primitives', () => {
  it('renders a primary action with an accessible button name', () => {
    const markup = renderToStaticMarkup(<Button variant="primary">Create workspace</Button>)
    expect(markup).toContain('button')
    expect(markup).toContain('Create workspace')
    expect(markup).toContain('button-primary')
    expect(markup).toContain('type="button"')
  })

  it('keeps loading actions disabled and exposes busy state', () => {
    const markup = renderToStaticMarkup(<Button loading>Save</Button>)
    expect(markup).toContain('disabled=""')
    expect(markup).toContain('aria-busy="true"')
  })

  it('renders explicit error recovery state', () => {
    const markup = renderWithLocale(<StateView state="error" description="Service unavailable" onRetry={() => undefined} />)
    expect(markup).toContain('Service unavailable')
    expect(markup).toContain('Retry')
  })

  it('renders dialogs with an accessible title and close action', () => {
    const markup = renderWithLocale(<Modal title="Invite member" onClose={() => undefined}>Form</Modal>)
    expect(markup).toContain('role="dialog"')
    expect(markup).toMatch(/aria-labelledby="[^"]+"/)
    expect(markup).toContain('Invite member')
    expect(markup).toContain('aria-label="Close"')
    expect(markup).toContain('tabindex="-1"')
  })
})

describe('console UI primitives — interaction', () => {
  /* ---------------------------------------------------------------------- */
  /* IconButton                                                              */
  /* ---------------------------------------------------------------------- */
  it('IconButton renders children and applies the icon-button class', () => {
    const { container } = renderWithProviders(
      <IconButton label="Open menu">
        <span aria-hidden="true">≡</span>
      </IconButton>,
    )
    const button = screen.getByRole('button', { name: 'Open menu' })
    expect(button).toHaveClass('icon-button')
    expect(container.querySelector('.icon-button')).not.toBeNull()
    expect(screen.getByText('≡')).toBeInTheDocument()
  })

  it('IconButton exposes aria-label and title for accessibility', () => {
    renderWithProviders(<IconButton label="Close panel"><span>X</span></IconButton>)
    const button = screen.getByRole('button', { name: 'Close panel' })
    expect(button).toHaveAttribute('aria-label', 'Close panel')
    expect(button).toHaveAttribute('title', 'Close panel')
  })

  it('IconButton forwards the disabled state to the underlying button', () => {
    renderWithProviders(<IconButton label="Delete" disabled><span>🗑</span></IconButton>)
    const button = screen.getByRole('button', { name: 'Delete' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('disabled')
  })

  it('IconButton fires onClick when clicked', () => {
    const onClick = vi.fn()
    renderWithProviders(<IconButton label="Refresh" onClick={onClick}><span>r</span></IconButton>)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  /* ---------------------------------------------------------------------- */
  /* Badge                                                                  */
  /* ---------------------------------------------------------------------- */
  it('Badge applies the correct tone class for every tone variant', () => {
    const { container } = renderWithProviders(
      <>
        <Badge tone="info">info</Badge>
        <Badge tone="success">success</Badge>
        <Badge tone="warning">warning</Badge>
        <Badge tone="danger">danger</Badge>
        <Badge tone="neutral">neutral</Badge>
      </>,
    )
    expect(container.querySelector('.badge-info')).not.toBeNull()
    expect(container.querySelector('.badge-success')).not.toBeNull()
    expect(container.querySelector('.badge-warning')).not.toBeNull()
    expect(container.querySelector('.badge-danger')).not.toBeNull()
    expect(container.querySelector('.badge-neutral')).not.toBeNull()
  })

  it('Badge defaults to badge-neutral when no tone is provided', () => {
    const { container } = renderWithProviders(<Badge>Default</Badge>)
    expect(container.querySelector('.badge-neutral')).not.toBeNull()
    expect(container.querySelector('.badge-info')).toBeNull()
    expect(screen.getByText('Default')).toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------- */
  /* Panel                                                                  */
  /* ---------------------------------------------------------------------- */
  it('Panel renders title, subtitle, actions and body children', () => {
    renderWithProviders(
      <Panel title="Agents" subtitle="3 active" actions={<button type="button">New</button>}>
        <p>Row content</p>
      </Panel>,
    )
    expect(screen.getByRole('heading', { level: 2, name: 'Agents' })).toBeInTheDocument()
    expect(screen.getByText('3 active')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New' })).toBeInTheDocument()
    expect(screen.getByText('Row content')).toBeInTheDocument()
  })

  it('Panel omits the header and renders body only when no title is provided', () => {
    const { container } = renderWithProviders(<Panel><p>Body only</p></Panel>)
    expect(container.querySelector('.panel-header')).toBeNull()
    expect(container.querySelector('.panel-body')).not.toBeNull()
    expect(screen.getByText('Body only')).toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------- */
  /* Field                                                                  */
  /* ---------------------------------------------------------------------- */
  it('Field renders the label and child input without a hint', () => {
    const { container } = renderWithProviders(
      <Field label="Workspace name"><input type="text" defaultValue="Acme" /></Field>,
    )
    expect(screen.getByText('Workspace name')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Acme')).toBeInTheDocument()
    expect(container.querySelector('small')).toBeNull()
  })

  it('Field renders the hint text when provided', () => {
    renderWithProviders(
      <Field label="Work email" hint="Use your company email"><input type="email" /></Field>,
    )
    expect(screen.getByText('Use your company email')).toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------- */
  /* SearchBox (controlled)                                                 */
  /* ---------------------------------------------------------------------- */
  it('SearchBox renders the controlled value and a custom placeholder', () => {
    renderWithProviders(<SearchBox value="abc" onChange={() => undefined} placeholder="Find agents" />)
    const input = screen.getByRole('searchbox')
    expect(input).toHaveValue('abc')
    expect(input).toHaveAttribute('placeholder', 'Find agents')
    expect(input).toHaveAttribute('aria-label', 'Find agents')
  })

  it('SearchBox calls onChange with the new value when the user types', () => {
    const onChange = vi.fn()
    renderWithProviders(<SearchBox value="" onChange={onChange} placeholder="Search" />)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'workama' } })
    expect(onChange).toHaveBeenCalledWith('workama')
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('SearchBox falls back to the localized default placeholder', () => {
    renderWithProviders(<SearchBox value="" onChange={() => undefined} />)
    const input = screen.getByRole('searchbox')
    // 默认 locale 在 jsdom 下推断为 en-US，t('ui.search') === 'Search'
    expect(input).toHaveAttribute('placeholder', 'Search')
  })

  /* ---------------------------------------------------------------------- */
  /* DataTable                                                              */
  /* ---------------------------------------------------------------------- */
  it('DataTable renders column headers and row data from children', () => {
    renderWithProviders(
      <DataTable headers={['Name', 'Status']} caption="Agent list">
        <tr><td>Alpha</td><td>active</td></tr>
      </DataTable>,
    )
    const table = screen.getByRole('table')
    expect(table).toHaveAttribute('aria-label', 'Agent list')
    expect(screen.getByRole('columnheader', { name: 'Name' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Status' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'Alpha' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'active' })).toBeInTheDocument()
  })

  it('DataTable renders a caption and an empty body when no rows are provided', () => {
    const { container } = renderWithProviders(<DataTable headers={['Name']} caption="Empty list" />)
    expect(screen.getByRole('table')).toHaveAttribute('aria-label', 'Empty list')
    expect(container.querySelectorAll('tbody tr')).toHaveLength(0)
    expect(screen.getByText('Empty list')).toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------- */
  /* Kpi                                                                    */
  /* ---------------------------------------------------------------------- */
  it('Kpi renders label, value, trend and icon', () => {
    renderWithProviders(
      <Kpi label="Tokens" value="1.2k" trend="+5%" icon={<i data-testid="kpi-icon">★</i>} />,
    )
    expect(screen.getByText('Tokens')).toBeInTheDocument()
    expect(screen.getByText('1.2k')).toBeInTheDocument()
    expect(screen.getByText('+5%')).toBeInTheDocument()
    expect(screen.getByTestId('kpi-icon')).toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------- */
  /* Toast                                                                  */
  /* ---------------------------------------------------------------------- */
  it('Toast renders the message with role=status and a dismiss button', () => {
    renderWithProviders(<Toast message="Saved successfully" onClose={() => undefined} />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByText('Saved successfully')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('Toast calls onClose when the dismiss button is clicked', () => {
    const onClose = vi.fn()
    renderWithProviders(<Toast message="Saved" onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  /* ---------------------------------------------------------------------- */
  /* Status                                                                 */
  /* ---------------------------------------------------------------------- */
  it('Status maps success-like values to a status-success dot', () => {
    const { container } = renderWithProviders(<Status value="active" />)
    expect(container.querySelector('.status-dot.status-success')).not.toBeNull()
    expect(screen.getByText('active')).toBeInTheDocument()
  })

  it('Status maps failed values to a status-danger dot', () => {
    const { container } = renderWithProviders(<Status value="failed" />)
    expect(container.querySelector('.status-dot.status-danger')).not.toBeNull()
  })

  it('Status falls back to the localized unknown text when no value is provided', () => {
    const { container } = renderWithProviders(<Status />)
    expect(container.querySelector('.status-dot.status-neutral')).not.toBeNull()
    // 默认 en-US：t('ui.unknown') === 'unknown'
    expect(screen.getByText('unknown')).toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------- */
  /* EmptyAction (needs Router)                                             */
  /* ---------------------------------------------------------------------- */
  it('EmptyAction renders a router link with the label and target href', () => {
    renderWithProviders(<EmptyAction to="/agents" label="Add agent" />, { router: true })
    const link = screen.getByRole('link', { name: 'Add agent' })
    expect(link).toHaveAttribute('href', '/agents')
  })

  it('EmptyAction renders the chevron icon and button-primary class', () => {
    const { container } = renderWithProviders(<EmptyAction to="/x" label="Go" />, { router: true })
    expect(container.querySelector('.button-primary')).not.toBeNull()
    expect(container.querySelector('svg')).not.toBeNull()
  })

  /* ---------------------------------------------------------------------- */
  /* Modal interactions                                                     */
  /* ---------------------------------------------------------------------- */
  it('Modal calls onClose when the Escape key is pressed', () => {
    const onClose = vi.fn()
    renderWithProviders(<Modal title="Invite" onClose={onClose}>Form</Modal>)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Modal calls onClose when the backdrop is clicked but not when content is clicked', () => {
    const onClose = vi.fn()
    const { container } = renderWithProviders(<Modal title="Invite" onClose={onClose}>Form</Modal>)
    const backdrop = container.querySelector('.modal-backdrop') as HTMLElement
    const modal = container.querySelector('.modal') as HTMLElement
    fireEvent.mouseDown(modal)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.mouseDown(backdrop)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Modal traps Tab focus: Tab on the last focusable returns focus to the first', () => {
    renderWithProviders(
      <Modal title="Invite" onClose={() => undefined}>
        <button type="button">Save</button>
      </Modal>,
    )
    const closeBtn = screen.getByRole('button', { name: 'Close' })
    const saveBtn = screen.getByRole('button', { name: 'Save' })
    saveBtn.focus()
    expect(document.activeElement).toBe(saveBtn)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(closeBtn)
  })

  /* ---------------------------------------------------------------------- */
  /* Button variants & icon                                                 */
  /* ---------------------------------------------------------------------- */
  it('Button renders the correct class for each variant', () => {
    const { container } = renderWithProviders(
      <>
        <Button variant="primary">Primary</Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="danger">Danger</Button>
      </>,
    )
    expect(container.querySelector('.button-primary')).not.toBeNull()
    expect(container.querySelector('.button-secondary')).not.toBeNull()
    expect(container.querySelector('.button-ghost')).not.toBeNull()
    expect(container.querySelector('.button-danger')).not.toBeNull()
  })

  it('Button renders the provided icon when not loading', () => {
    renderWithProviders(<Button icon={<i data-testid="btn-icon" aria-hidden="true" />}>Save</Button>)
    expect(screen.getByTestId('btn-icon')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: 'Save' })
    expect(button).not.toBeDisabled()
    expect(button).not.toHaveAttribute('aria-busy')
  })
})
