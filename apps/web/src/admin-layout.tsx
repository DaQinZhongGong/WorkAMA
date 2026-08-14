/**
 * Admin 后台布局：侧边栏导航 + 顶部栏 + Outlet。
 * 所有 /admin/* 页面共用此布局。
 */
import { type ReactNode } from 'react'
import { useEffect } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { type MessageKey } from '@workama/i18n'
import { LocaleToggle, useLocale } from './locale'
import {
  Activity,
  Bell,
  BookOpen,
  Bot,
  Boxes,
  Database,
  FileText,
  Files,
  Gift,
  GitBranch,
  HardDrive,
  LayoutDashboard,
  Megaphone,
  Network,
  Palette,
  Plug,
  Receipt,
  Repeat,
  ScrollText,
  Sparkles,
  Wrench,
  Workflow,
} from 'lucide-react'

type NavEntry = { to: string; labelKey: MessageKey; icon: typeof Activity }

const NAV_ITEMS: NavEntry[] = [
  { to: '/admin', labelKey: 'admin.nav.dashboard', icon: LayoutDashboard },
  { to: '/admin/workspaces', labelKey: 'admin.nav.workspaces', icon: BookOpen },
  { to: '/admin/assistants', labelKey: 'admin.nav.assistants', icon: Bot },
  { to: '/admin/workflows', labelKey: 'admin.nav.workflows', icon: Workflow },
  { to: '/admin/knowledge-bases', labelKey: 'admin.nav.knowledge', icon: Database },
  { to: '/admin/devices', labelKey: 'admin.nav.devices', icon: HardDrive },
  { to: '/admin/billing', labelKey: 'admin.nav.billing', icon: Receipt },
  { to: '/admin/audit-logs', labelKey: 'admin.nav.audit', icon: ScrollText },
  { to: '/admin/mcp-tools', labelKey: 'admin.nav.mcp', icon: Wrench },
  { to: '/admin/notifications', labelKey: 'admin.nav.notifications', icon: Bell },
  { to: '/admin/files', labelKey: 'admin.nav.files', icon: Files },
  { to: '/admin/memory-vectors', labelKey: 'admin.nav.memory', icon: Activity },
  { to: '/admin/free-providers', labelKey: 'admin.nav.freeProviders', icon: Gift },
  { to: '/admin/connectors', labelKey: 'admin.nav.connectors', icon: Plug },
  { to: '/admin/automations', labelKey: 'admin.nav.automations', icon: Repeat },
  { to: '/admin/push', labelKey: 'admin.nav.push', icon: Megaphone },
  { to: '/admin/design-projects', labelKey: 'admin.nav.designProjects', icon: Palette },
  { to: '/admin/external-apps', labelKey: 'admin.nav.externalApps', icon: Network },
  { to: '/admin/agent-planner', labelKey: 'admin.nav.agentPlanner', icon: GitBranch },
]

export function AdminLayout(): ReactNode {
  // v7.264: /admin/* 路由动态 title + meta description（与 ConsoleLayout 对齐），
  // 修复 Lighthouse meta-description audit（/admin 此前无 description）。
  const { t } = useLocale()
  const location = useLocation()
  useEffect(() => {
    const path = location.pathname
    const segment = path.split('/').filter(Boolean)[1] || 'dashboard'
    const TITLE_KEYS: Record<string, MessageKey> = {
      dashboard: 'admin.nav.dashboard', workspaces: 'admin.nav.workspaces', assistants: 'admin.nav.assistants',
      workflows: 'admin.nav.workflows', 'knowledge-bases': 'admin.nav.knowledge', devices: 'admin.nav.devices',
      billing: 'admin.nav.billing', 'audit-logs': 'admin.nav.audit', 'mcp-tools': 'admin.nav.mcp',
      notifications: 'admin.nav.notifications', files: 'admin.nav.files', 'memory-vectors': 'admin.nav.memory',
      'free-providers': 'admin.nav.freeProviders', connectors: 'admin.nav.connectors', automations: 'admin.nav.automations',
      push: 'admin.nav.push', 'design-projects': 'admin.nav.designProjects', 'external-apps': 'admin.nav.externalApps',
      'agent-planner': 'admin.nav.agentPlanner',
    }
    const label = t(TITLE_KEYS[segment] || 'admin.nav.dashboard')
    document.title = `WorkAMA — ${label}`
    let meta = document.querySelector('meta[name="description"]') as HTMLMetaElement | null
    if (!meta) {
      meta = document.createElement('meta')
      meta.setAttribute('name', 'description')
      document.head.appendChild(meta)
    }
    meta.setAttribute('content', `WorkAMA Admin Console · ${label} · multi-tenant AI control plane with workspace governance, security, billing, audit and observability.`)
  }, [location.pathname])
  return (
    <div className="console-shell admin-shell" data-testid="admin-layout">
      <aside id="admin-sidebar" className="sidebar" aria-label={t('admin.nav.label')} data-testid="admin-sidebar">
        <div className="brand-row">
          <Link className="brand" to="/admin">
            <span className="brand-mark">
              <Sparkles size={16} />
            </span>
            <span className="brand-name">{t('admin.brand')}</span>
          </Link>
        </div>
        <nav data-testid="admin-nav">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/admin'}
                className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                data-testid={`admin-nav-${item.to.split('/').pop()}`}
              >
                <Icon size={17} strokeWidth={1.8} />
                <span>{t(item.labelKey)}</span>
              </NavLink>
            )
          })}
        </nav>
      </aside>
      <main className="console-main">
        <header className="topbar" data-testid="admin-topbar">
          <span className="trace-pill">{t('admin.console')}</span>
          <div className="topbar-spacer" /><LocaleToggle />
        </header>
        <div className="page-content">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

export default AdminLayout
