/**
 * Web 端 UI 出口。
 *
 * 通用展示组件统一来自 `@workama/ui` 共享包；PageHeader 保留本地实现，
 * 因为它携带 pageTitleKeys 业务键映射（页面标题 -> i18n key），
 * 这是 Web 控制台独有的业务逻辑，不应进入业务无关的共享 UI 库。
 */
import type { ReactNode } from 'react'
import { useLocale } from './locale'
import type { MessageKey } from '@workama/i18n'

export {
  Badge,
  Button,
  DataTable,
  EmptyAction,
  Field,
  IconButton,
  Kpi,
  Modal,
  Panel,
  SearchBox,
  StateView,
  Status,
  Toast,
} from '@workama/ui'

const pageTitleKeys: Record<string, MessageKey> = {
  Agents: 'page.agents',
  'API keys': 'page.apiKeys',
  Applications: 'page.applications',
  'Audit & evidence': 'page.auditEvidence',
  Automations: 'page.automations',
  Billing: 'page.billing',
  'Code workspace': 'page.codeWorkspace',
  'Compliance center': 'page.compliance',
  'Design workspace': 'page.designWorkspace',
  'Devices & passkeys': 'page.devices',
  'Enterprise identity': 'page.enterpriseIdentity',
  'Global search': 'page.globalSearch',
  'Import diagnostics': 'page.importDiagnostics',
  Knowledge: 'page.knowledge',
  Members: 'page.members',
  Memory: 'page.memory',
  'Notification templates': 'page.notificationTemplates',
  Notifications: 'page.notifications',
  Observability: 'page.observability',
  Operations: 'page.operations',
  'Privacy & data': 'page.privacy',
  'Retrieval evaluation': 'page.retrievalEvaluation',
  Security: 'page.security',
  'Studio integrations': 'page.studioIntegrations',
  'Template marketplace': 'page.templateMarketplace',
  'Tool approvals': 'page.toolApprovals',
  'Tool registry': 'page.toolRegistry',
  'Work plans': 'page.workPlans',
  Workflows: 'page.workflows',
  'Workspace settings': 'page.workspaceSettings',
  Workspaces: 'page.workspaces',
  'Your command center': 'page.commandCenter',
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
}) {
  const { t } = useLocale()
  const key = pageTitleKeys[title]
  return (
    <header className="page-header">
      <div>
        <div className="eyebrow">{eyebrow ?? t('ui.workamaConsole')}</div>
        <h1>{key ? t(key) : title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}
