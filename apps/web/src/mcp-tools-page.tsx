/**
 * MCP 工具：概览指标 + 工具列表 + 创建 + 调用结果展示。
 * 对应《520-Agent引擎与运行时设计》MCP 客户端与工具调用。
 */
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Plug, Plus, Send, Terminal, Wrench, Zap } from 'lucide-react'
import { api, errorMessage } from './api'
import { AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Badge, Button, Field, Kpi, Panel, SearchBox, StateView, Status } from './ui'
import { useLocale } from './locale'

type McpTool = {
  id: string
  name: string
  description?: string
  server?: string
  status?: string
  transport?: string
  capabilities?: string[]
}

const SERVER_PRESETS = ['stdio', 'sse', 'http', 'local']
const TRANSPORT_TONE: Record<string, string> = {
  stdio: 'badge-info',
  sse: 'badge-warning',
  http: 'badge-success',
  local: 'badge-neutral',
}

/** 取名称前两个字符作为头像标识。 */
function initial(name: string): string {
  const trimmed = name.trim()
  return trimmed ? trimmed.slice(0, 2).toUpperCase() : '·'
}

export default function AdminMcpToolsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<McpTool>(
    '/api/v1/mcp/tools',
  )
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [server, setServer] = useState('')
  const [description, setDescription] = useState('')
  const [formError, setFormError] = useState('')
  const [invokeState, setInvokeState] = useState<{ toolId: string; state: 'running' | 'done' | 'error'; message: string } | null>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  const { t } = useLocale()

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((item) =>
      `${item.name} ${item.server ?? ''} ${item.description ?? ''}`
        .toLowerCase()
        .includes(keyword),
    )
  }, [items, query])

  const stats = useMemo(() => {
    const servers = new Set(items.map((item) => (item.server ?? 'local').toLowerCase())).size
    const enabled = items.filter((item) => String(item.status ?? '').toLowerCase() === 'enabled').length
    return { servers, enabled }
  }, [items])

  function focusCreate() {
    nameRef.current?.focus()
    nameRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await create({ name, server, description })
      setName('')
      setServer('')
      setDescription('')
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.createFailed'))
    }
  }

  async function invoke(tool: McpTool) {
    setInvokeState({ toolId: tool.id, state: 'running', message: `${t('admin.mcp.invoking')} ${tool.name}` })
    try {
      const result = await api.post<{ result?: string }>(
        `/api/v1/mcp/tools/${encodeURIComponent(tool.id)}/invoke`,
      )
      setInvokeState({
        toolId: tool.id,
        state: 'done',
        message: result?.result ?? t('admin.mcp.invokeDone'),
      })
    } catch (caught) {
      setInvokeState({
        toolId: tool.id,
        state: 'error',
        message: errorMessage(caught, t('common.invokeFailed')),
      })
    }
  }

  return (
    <AdminPageShell
      title={t('admin.mcp.title')}
      subtitle={t('admin.mcp.subtitle')}
      testId="mcp-tools-page"
      loading={loading}
      error={error}
      onRetry={reload}
      actions={
        <Button variant="primary" icon={<Plus size={15} />} onClick={focusCreate} data-testid="mcp-new">
          {t('admin.mcp.new')}
        </Button>
      }
    >
      <div className="kpi-grid">
        <Kpi
          label={t('admin.mcp.kpi.total')}
          value={String(items.length).padStart(2, '0')}
          icon={<Wrench size={18} />}
          trend={t('admin.mcp.kpi.total.trend')}
        />
        <Kpi
          label={t('admin.mcp.kpi.enabled')}
          value={String(stats.enabled).padStart(2, '0')}
          icon={<Zap size={18} />}
          trend={t('admin.mcp.kpi.enabled.trend')}
        />
        <Kpi
          label={t('admin.mcp.kpi.servers')}
          value={String(stats.servers).padStart(2, '0')}
          icon={<Plug size={18} />}
          trend={t('admin.mcp.kpi.servers.trend')}
        />
        <Kpi
          label={t('admin.mcp.kpi.transport')}
          value={String(
            new Set(items.map((item) => (item.transport ?? 'stdio').toLowerCase())).size,
          ).padStart(2, '0')}
          icon={<Terminal size={18} />}
          trend={t('admin.mcp.kpi.transport.trend')}
        />
      </div>

      <div className="ops-grid">
        <Panel
          title={t('admin.mcp.list.title')}
          subtitle={t('admin.mcp.list.subtitle')}
          actions={<SearchBox value={query} onChange={setQuery} placeholder={t('admin.mcp.search')} />}
        >
          {items.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.mcp.empty.title')}
              description={t('admin.mcp.empty.desc')}
            />
          ) : filtered.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.mcp.emptyFiltered.title')}
              description={t('admin.mcp.emptyFiltered.desc')}
            />
          ) : (
            <div className="resource-grid" data-testid="mcp-list">
              {filtered.map((item) => (
                <div key={item.id} className="resource-card" data-testid="mcp-item">
                  <span className="avatar" aria-hidden="true">
                    {initial(item.name)}
                  </span>
                  <div className="resource-main">
                    <strong>{item.name}</strong>
                    {item.description && <p>{item.description}</p>}
                    <span>
                      {item.server ?? 'local'}
                      {item.transport ? ` · ${item.transport}` : ''}
                    </span>
                  </div>
                  <div className="panel-actions-inline">
                    {item.transport && (
                      <span
                        className={`badge ${TRANSPORT_TONE[(item.transport ?? '').toLowerCase()] ?? 'badge-neutral'}`}
                      >
                        {item.transport}
                      </span>
                    )}
                    {item.status && <Status value={item.status} />}
                  </div>
                  <div className="knowledge-actions">
                    <Button
                      variant="primary"
                      onClick={() => void invoke(item)}
                      data-testid={`mcp-invoke-${item.id}`}
                      icon={<Send size={14} />}
                    >
                      {t('admin.mcp.invoke')}
                    </Button>
                    <DeleteButton
                      testId={`mcp-delete-${item.id}`}
                      onDelete={() => void remove(item.id)}
                      busy={busy}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}

          {invokeState && (
            <div
              className={`alert ${
                invokeState.state === 'error'
                  ? 'alert-error'
                  : invokeState.state === 'running'
                    ? 'alert-info'
                    : 'alert-success'
              }`}
              data-testid={`mcp-invoke-result-${invokeState.toolId}`}
              data-state={invokeState.state}
              style={{ marginTop: 16 }}
            >
              <strong>
                <Badge tone={invokeState.state === 'error' ? 'danger' : invokeState.state === 'running' ? 'info' : 'success'}>
                  {invokeState.state}
                </Badge>{' '}
                {invokeState.message}
              </strong>
            </div>
          )}
        </Panel>

        <Panel
          title={t('admin.mcp.create.title')}
          subtitle={t('admin.mcp.create.subtitle')}
        >
          <form className="form-stack" data-testid="mcp-create" onSubmit={submitCreate}>
            <Field
              label={t('admin.mcp.field.name')}
              hint={t('admin.mcp.field.name.hint')}
            >
              <input
                ref={nameRef}
                name="name"
                value={name}
                placeholder={t('admin.mcp.field.name.placeholder')}
                onChange={(e) => setName(e.target.value)}
                data-testid="mcp-create-name"
              />
            </Field>
            <Field
              label={t('admin.mcp.field.server')}
              hint={t('admin.mcp.field.server.hint')}
            >
              <input
                name="server"
                list="mcp-server-presets"
                value={server}
                placeholder={t('admin.mcp.field.server.placeholder')}
                onChange={(e) => setServer(e.target.value)}
                data-testid="mcp-create-server"
              />
            </Field>
            <datalist id="mcp-server-presets">
              {SERVER_PRESETS.map((preset) => (
                <option key={preset} value={preset} />
              ))}
            </datalist>
            <Field
              label={t('admin.mcp.field.description')}
              hint={t('admin.mcp.field.description.hint')}
            >
              <textarea
                name="description"
                rows={3}
                value={description}
                placeholder={t('admin.mcp.field.description.placeholder')}
                onChange={(e) => setDescription(e.target.value)}
                data-testid="mcp-create-description"
              />
            </Field>
            {formError && (
              <div className="alert alert-error" role="alert">
                {formError}
              </div>
            )}
            <Button
              type="submit"
              variant="primary"
              loading={busy}
              icon={<Plus size={15} />}
              data-testid="mcp-create-submit"
            >
              {t('common.create')}
            </Button>
          </form>
        </Panel>
      </div>
    </AdminPageShell>
  )
}