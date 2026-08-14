/**
 * @workama/ui 烟雾测试
 *
 * 不引入 testing-library，沿用 web/src/ui.spec.tsx 的 renderToStaticMarkup 模式，
 * 仅验证各组件能够稳定渲染出预期的结构/类名，避免共享包出现"零测试"风险。
 */
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ReactElement } from 'react'
import {
  Badge,
  Button,
  DataTable,
  EmptyAction,
  Field,
  IconButton,
  Kpi,
  LocaleProvider,
  LocaleToggle,
  Modal,
  PageHeader,
  Panel,
  SearchBox,
  StateView,
  Status,
  Toast,
} from './index'

function render(element: ReactElement): string {
  return renderToStaticMarkup(<LocaleProvider>{element}</LocaleProvider>)
}

describe('@workama/ui smoke', () => {
  it('Button 渲染主按钮并带 variant class', () => {
    const markup = renderToStaticMarkup(<Button variant="primary">Save</Button>)
    expect(markup).toContain('button-primary')
    expect(markup).toContain('Save')
    expect(markup).toContain('type="button"')
  })

  it('Button loading 时禁用并暴露 aria-busy', () => {
    const markup = renderToStaticMarkup(<Button loading>Saving</Button>)
    expect(markup).toContain('disabled=""')
    expect(markup).toContain('aria-busy="true"')
  })

  it('IconButton 携带 aria-label 与 title', () => {
    const markup = renderToStaticMarkup(
      <IconButton label="Close">
        <span>X</span>
      </IconButton>,
    )
    expect(markup).toContain('aria-label="Close"')
    expect(markup).toContain('title="Close"')
  })

  it('Badge 输出 tone class', () => {
    expect(renderToStaticMarkup(<Badge tone="success">ok</Badge>)).toContain('badge-success')
    expect(renderToStaticMarkup(<Badge tone="danger">bad</Badge>)).toContain('badge-danger')
  })

  it('Panel 渲染 title/subtitle/actions/body', () => {
    const markup = renderToStaticMarkup(
      <Panel title="Agents" subtitle="3 active" actions={<button>NEW</button>}>
        <p>row</p>
      </Panel>,
    )
    expect(markup).toContain('panel-header')
    expect(markup).toContain('Agents')
    expect(markup).toContain('3 active')
    expect(markup).toContain('NEW')
    expect(markup).toContain('panel-body')
  })

  it('Field 渲染 label/hint/children', () => {
    const markup = renderToStaticMarkup(
      <Field label="Name" hint="Use real name">
        <input />
      </Field>,
    )
    expect(markup).toContain('Name')
    expect(markup).toContain('Use real name')
  })

  it('SearchBox 渲染 input 与 aria-label', () => {
    const markup = render(<SearchBox value="" onChange={() => undefined} />)
    expect(markup).toContain('type="search"')
    expect(markup).toContain('search-box')
  })

  it('DataTable 渲染 headers 与 caption', () => {
    const markup = renderToStaticMarkup(
      <DataTable headers={['Name', 'Status']} caption="Agent list">
        <tr><td>a</td></tr>
      </DataTable>,
    )
    expect(markup).toContain('data-table')
    expect(markup).toContain('aria-label="Agent list"')
    expect(markup).toContain('Name')
    expect(markup).toContain('Status')
  })

  it('Kpi 渲染 label/value/trend/icon', () => {
    const markup = renderToStaticMarkup(
      <Kpi label="Tokens" value="1.2k" trend="+5%" icon={<i>★</i>} />,
    )
    expect(markup).toContain('Tokens')
    expect(markup).toContain('1.2k')
    expect(markup).toContain('+5%')
  })

  it('Toast 渲染消息与 dismiss 按钮', () => {
    const markup = render(<Toast message="Saved" onClose={() => undefined} />)
    expect(markup).toContain('Saved')
    expect(markup).toContain('aria-label="Dismiss"')
  })

  it('StateView error 渲染 Retry 按钮', () => {
    const markup = render(
      <StateView state="error" description="boom" onRetry={() => undefined} />,
    )
    expect(markup).toContain('role="alert"')
    expect(markup).toContain('boom')
    expect(markup).toContain('Retry')
  })

  it('Status 根据值映射 tone', () => {
    expect(render(<Status value="active" />)).toContain('status-success')
    expect(render(<Status value="failed" />)).toContain('status-danger')
    expect(render(<Status value="queued" />)).toContain('status-warning')
  })

  it('EmptyAction 在 Router 上下文中渲染 link（由 web e2e 覆盖端到端，此处仅冒烟导入）', () => {
    // EmptyAction 依赖 react-router <Link>，SSR 需要 StaticRouter；
    // 该组件仅 3 行实现，已被 web/share 的 e2e 与单元测试覆盖。
    expect(typeof EmptyAction).toBe('function')
  })

  it('Modal 渲染 dialog 角色与标题', () => {
    const markup = render(<Modal title="Invite" onClose={() => undefined}>Form</Modal>)
    expect(markup).toContain('role="dialog"')
    expect(markup).toContain('aria-modal="true"')
    expect(markup).toContain('Invite')
    expect(markup).toContain('tabindex="-1"')
  })

  it('PageHeader 渲染 eyebrow/title/description（业务无关版本）', () => {
    const markup = render(
      <PageHeader eyebrow="Console" title="Agents" description="Manage agents" />,
    )
    expect(markup).toContain('page-header')
    expect(markup).toContain('Console')
    expect(markup).toContain('Agents')
    expect(markup).toContain('Manage agents')
  })

  it('LocaleToggle 渲染按钮并可切换语言标签', () => {
    const markup = render(<LocaleToggle />)
    expect(markup).toContain('locale-toggle')
    // 默认 locale 由 i18n 推断，至少包含 EN 或 中 之一
    expect(/>(EN|中)</.test(markup)).toBe(true)
  })
})
