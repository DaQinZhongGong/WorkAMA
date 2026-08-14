import { lazy, Suspense, useEffect, useState, type ReactNode } from 'react'
import { Navigate, Outlet, Route, Routes, Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, ArrowUpRight, Check, Globe2, LockKeyhole, ShieldCheck, Sparkles } from 'lucide-react'
import { useAuth } from './auth'
import { useLocale } from './locale'
import { ConsoleLayout } from './layout'
import { api } from './api'
import { AuthPage, UtilityPage } from './pages'
import { Badge, Button, Field, StateView } from './ui'
const ChatPage = lazy(() => import('./features/chat/ChatPage'))
const KnowledgePage = lazy(() => import('./pages').then(m => ({ default: m.KnowledgePage })))
const DesignPage = lazy(() => import('./resource-pages').then(m => ({ default: m.DesignPage })))
const OperationsPage = lazy(() => import('./resource-pages').then(m => ({ default: m.OperationsPage })))
const SearchPage = lazy(() => import('./resource-pages').then(m => ({ default: m.SearchPage })))
const WorkflowPage = lazy(() => import('./resource-pages').then(m => ({ default: m.WorkflowPage })))
const AgentDetailPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AgentDetailPage })))
const AgentsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AgentsPage })))
const AgentToolsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AgentToolsPage })))
const ApiKeysPage = lazy(() => import('./domain-pages').then(m => ({ default: m.ApiKeysPage })))
const AppStudioDetailPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AppStudioDetailPage })))
const AppStudioPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AppStudioPage })))
const AppStudioRunsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AppStudioRunsPage })))
const AutomationsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AutomationsPage })))
const AuditPage = lazy(() => import('./domain-pages').then(m => ({ default: m.AuditPage })))
const BillingPage = lazy(() => import('./domain-pages').then(m => ({ default: m.BillingPage })))
const CodePage = lazy(() => import('./domain-pages').then(m => ({ default: m.CodePage })))
const CompliancePage = lazy(() => import('./domain-pages').then(m => ({ default: m.CompliancePage })))
const DevicesPage = lazy(() => import('./domain-pages').then(m => ({ default: m.DevicesPage })))
const EnterpriseIdentityPage = lazy(() => import('./domain-pages').then(m => ({ default: m.EnterpriseIdentityPage })))
const GatewayConsolePage = lazy(() => import('./domain-pages').then(m => ({ default: m.GatewayConsolePage })))
const GatewayImportDiagnosticsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.GatewayImportDiagnosticsPage })))
const MarketplacePage = lazy(() => import('./domain-pages').then(m => ({ default: m.MarketplacePage })))
const MemoryPage = lazy(() => import('./domain-pages').then(m => ({ default: m.MemoryPage })))
const MembersPage = lazy(() => import('./domain-pages').then(m => ({ default: m.MembersPage })))
const NotificationsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.NotificationsPage })))
const ObservabilityPage = lazy(() => import('./domain-pages').then(m => ({ default: m.ObservabilityPage })))
const PlatformSupportPage = lazy(() => import('./domain-pages').then(m => ({ default: m.PlatformSupportPage })))
const PrivacyPage = lazy(() => import('./domain-pages').then(m => ({ default: m.PrivacyPage })))
const RagEvaluationDetailPage = lazy(() => import('./domain-pages').then(m => ({ default: m.RagEvaluationDetailPage })))
const RagEvaluationPage = lazy(() => import('./domain-pages').then(m => ({ default: m.RagEvaluationPage })))
const SecurityPage = lazy(() => import('./domain-pages').then(m => ({ default: m.SecurityPage })))
const StudioIntegrationsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.StudioIntegrationsPage })))
const ToolApprovalsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.ToolApprovalsPage })))
const WorkPage = lazy(() => import('./domain-pages').then(m => ({ default: m.WorkPage })))
const WorkspaceSettingsPage = lazy(() => import('./domain-pages').then(m => ({ default: m.WorkspaceSettingsPage })))
const WorkspacesPage = lazy(() => import('./domain-pages').then(m => ({ default: m.WorkspacesPage })))
const FreeProvidersPage = lazy(() => import('./free-providers-page'))
const DevelopersPage = lazy(() => import('./open-platform-docs-page'))
const AdminLayout = lazy(() => import('./admin-layout').then(m => ({ default: m.AdminLayout })))
const AdminDashboardPage = lazy(() => import('./admin-dashboard-page'))
const AdminWorkspacesPage = lazy(() => import('./workspaces-page'))
const AdminAssistantsPage = lazy(() => import('./assistants-page'))
const AdminWorkflowsPage = lazy(() => import('./workflows-page'))
const AdminKnowledgeBasesPage = lazy(() => import('./knowledge-bases-page'))
const AdminBillingPage = lazy(() => import('./billing-page'))
const AdminAuditLogsPage = lazy(() => import('./audit-logs-page'))
const AdminMcpToolsPage = lazy(() => import('./mcp-tools-page'))
const AdminFilesPage = lazy(() => import('./files-page'))
const AdminMemoryVectorsPage = lazy(() => import('./memory-vectors-page'))
const AdminConnectorsPage = lazy(() => import('./admin-connectors-page'))
const AdminAutomationsPage = lazy(() => import('./admin-automations-page'))
const AdminPushPage = lazy(() => import('./admin-push-page'))
const AdminDesignProjectsPage = lazy(() => import('./admin-design-projects-page'))
const AdminExternalAppsPage = lazy(() => import('./admin-external-apps-page'))
const AdminAgentPlannerPage = lazy(() => import('./admin-agent-planner-page'))
import type { MessageKey } from '@workama/i18n'

function RequireAuth() { const { authenticated, loading } = useAuth(); const location = useLocation(); const { t } = useLocale(); if (loading) return <StateView state="loading" title={t('public.preparingWorkspace')} />; if (!authenticated) return <Navigate to={`/login?redirect=${encodeURIComponent(location.pathname)}`} replace />; return <Outlet /> }
type PublicPlatformInfo = { overall_status: string; services: { name: string; status: string }[]; privacy: { classification_coverage_percent: number; registered_tables: number; data_classes: string[]; customer_content_training: boolean }; controls: string[] }
function usePublicPlatformInfo() { const { t } = useLocale(); const [data, setData] = useState<PublicPlatformInfo | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const load = () => { setLoading(true); setError(''); void api.get<PublicPlatformInfo>('/api/v1/public/platform-info').then(setData).catch((caught) => setError(caught instanceof Error ? caught.message : t('public.status.unavailable'))).finally(() => setLoading(false)) }; useEffect(load, []); return { data, loading, error, reload: load } }
function publicTone(value: string): 'success' | 'warning' | 'danger' { return value === 'operational' ? 'success' : value === 'degraded' ? 'warning' : 'danger' }
function PublicFrame({ children }: { children: ReactNode }) { const { t } = useLocale(); return <div className="public-shell"><header className="public-topbar"><Link className="brand dark" to="/"><span className="brand-mark"><Sparkles size={16} /></span>{t('public.brand')}</Link><nav><Link to="/pricing">{t('public.nav.pricing')}</Link><Link to="/docs">{t('public.nav.docs')}</Link><Link to="/status">{t('public.nav.status')}</Link><Link to="/trust">{t('public.nav.trust')}</Link><Link className="button button-primary" to="/login">{t('public.nav.openConsole')}</Link></nav></header>{children}<footer><span>{t('public.copyright')}</span><span>{t('public.builtFor')}</span></footer></div> }
function PublicStatusPage() { const { t } = useLocale(); const { data, loading, error, reload } = usePublicPlatformInfo(); return <PublicFrame><main className="public-main"><span className="eyebrow">{t('public.eyebrow')}</span><h1>{t('public.status.title')}</h1><p className="public-lead">{t('public.status.lead')}</p>{loading ? <StateView state="loading" title={t('public.status.checking')} /> : error ? <StateView state="error" description={error} onRetry={reload} /> : <><div className="public-panel status-summary"><div className="public-panel-icon"><Globe2 size={20} /></div><div><h2>{data?.overall_status === 'operational' ? t('public.status.allOperational') : t('public.status.needsAttention')}</h2><p>{t('public.status.componentHint')}</p></div><Badge tone={publicTone(data?.overall_status ?? 'unavailable')}>{data?.overall_status ?? 'unknown'}</Badge></div><div className="public-plans">{data?.services.map((service) => <div key={service.name}><strong>{service.name}</strong><span><Badge tone={publicTone(service.status)}>{service.status}</Badge></span><p>{t('public.status.monitoredHint')}</p></div>)}</div><div className="public-panel"><h2>{t('public.status.incidentTitle')}</h2><p>{t('public.status.incidentBody')}</p><Link className="button" to="/help">{t('public.status.openHelp')} <ArrowUpRight size={15} /></Link></div></>}</main></PublicFrame> }
const helpFaqs: ReadonlyArray<{ categoryKey: MessageKey; qKey: MessageKey; aKey: MessageKey }> = [
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.001.q', aKey: 'help.faq.001.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.002.q', aKey: 'help.faq.002.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.003.q', aKey: 'help.faq.003.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.004.q', aKey: 'help.faq.004.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.005.q', aKey: 'help.faq.005.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.006.q', aKey: 'help.faq.006.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.007.q', aKey: 'help.faq.007.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.008.q', aKey: 'help.faq.008.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.009.q', aKey: 'help.faq.009.a' },
  { categoryKey: 'public.help.category.chat', qKey: 'help.faq.010.q', aKey: 'help.faq.010.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.011.q', aKey: 'help.faq.011.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.012.q', aKey: 'help.faq.012.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.013.q', aKey: 'help.faq.013.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.014.q', aKey: 'help.faq.014.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.015.q', aKey: 'help.faq.015.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.016.q', aKey: 'help.faq.016.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.017.q', aKey: 'help.faq.017.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.018.q', aKey: 'help.faq.018.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.019.q', aKey: 'help.faq.019.a' },
  { categoryKey: 'public.help.category.knowledge', qKey: 'help.faq.020.q', aKey: 'help.faq.020.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.021.q', aKey: 'help.faq.021.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.022.q', aKey: 'help.faq.022.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.023.q', aKey: 'help.faq.023.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.024.q', aKey: 'help.faq.024.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.025.q', aKey: 'help.faq.025.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.026.q', aKey: 'help.faq.026.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.027.q', aKey: 'help.faq.027.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.028.q', aKey: 'help.faq.028.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.029.q', aKey: 'help.faq.029.a' },
  { categoryKey: 'public.help.category.workflows', qKey: 'help.faq.030.q', aKey: 'help.faq.030.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.031.q', aKey: 'help.faq.031.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.032.q', aKey: 'help.faq.032.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.033.q', aKey: 'help.faq.033.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.034.q', aKey: 'help.faq.034.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.035.q', aKey: 'help.faq.035.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.036.q', aKey: 'help.faq.036.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.037.q', aKey: 'help.faq.037.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.038.q', aKey: 'help.faq.038.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.039.q', aKey: 'help.faq.039.a' },
  { categoryKey: 'public.help.category.governance', qKey: 'help.faq.040.q', aKey: 'help.faq.040.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.041.q', aKey: 'help.faq.041.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.042.q', aKey: 'help.faq.042.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.043.q', aKey: 'help.faq.043.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.044.q', aKey: 'help.faq.044.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.045.q', aKey: 'help.faq.045.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.046.q', aKey: 'help.faq.046.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.047.q', aKey: 'help.faq.047.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.048.q', aKey: 'help.faq.048.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.049.q', aKey: 'help.faq.049.a' },
  { categoryKey: 'public.help.category.platform', qKey: 'help.faq.050.q', aKey: 'help.faq.050.a' },
]
function PublicHelpPage() { const { t } = useLocale(); return <PublicFrame><main className="public-main"><span className="eyebrow">{t('public.eyebrow')}</span><h1>{t('public.help.title')}</h1><p className="public-lead">{t('public.help.lead')}</p><div className="docs-layout"><aside><strong>{t('public.help.title')}</strong><span>{t('public.help.answerCount')}</span><a className="active">{t('public.help.allTopics')}</a><a>{t('public.help.category.chat')}</a><a>{t('public.help.category.knowledge')}</a><a>{t('public.help.category.workflows')}</a><a>{t('public.help.category.governance')}</a><a>{t('public.help.category.platform')}</a></aside><article className="doc-article"><h2>{t('public.help.faqTitle')}</h2>{helpFaqs.map((item) => <details key={item.qKey}><summary><span>{t(item.categoryKey)}</span>{t(item.qKey)}</summary><p>{t(item.aKey)}</p></details>)}</article></div></main></PublicFrame> }
function PublicTrustPage() { const { t } = useLocale(); const { data, loading, error, reload } = usePublicPlatformInfo(); return <PublicFrame><main className="public-main"><span className="eyebrow">{t('public.eyebrow')}</span><h1>{t('public.trust.title')}</h1><p className="public-lead">{t('public.trust.lead')}</p>{loading ? <StateView state="loading" title={t('public.trust.loading')} /> : error ? <StateView state="error" description={error} onRetry={reload} /> : <div className="docs-layout"><aside><strong>{t('public.trust.title')}</strong><a className="active">{t('public.trust.summary')}</a><a>{t('public.trust.privacy')}</a><a>{t('public.trust.security')}</a><a>{t('public.trust.subprocessors')}</a></aside><article className="doc-article"><h2>{t('public.trust.currentSummary')}</h2><p>{t('public.trust.reviewNote')}</p><div className="public-panel"><h3>{t('public.trust.privacyPosture')}</h3><p>{data?.privacy.classification_coverage_percent}% {t('public.trust.coverage')} {data?.privacy.registered_tables} {t('public.trust.tables')}</p><p>{t('public.trust.customerTraining')} <strong>{data?.privacy.customer_content_training ? t('public.trust.enabled') : t('public.trust.disabled')}</strong>.</p><p>{t('public.trust.dataClasses')} {data?.privacy.data_classes.join(', ')}.</p></div><div className="public-panel"><h3>{t('public.trust.implementedControls')}</h3><ul>{data?.controls.map((control) => <li key={control}>{control}</li>)}</ul></div><div className="callout"><ShieldCheck size={16} /><span>{t('public.trust.evidenceNote')}</span></div></article></div>}</main></PublicFrame> }
function PublicPage({ page }: { page: 'terms' | 'privacy' | 'help' | 'status' | 'trust' | 'pricing' | 'docs' }) { const { t } = useLocale(); if (page === 'help') return <PublicHelpPage />; if (page === 'status') return <PublicStatusPage />; if (page === 'trust') return <PublicTrustPage />; const content: Record<string, [MessageKey, MessageKey]> = { terms: ['public.page.terms', 'public.page.termsDesc'], privacy: ['public.page.privacy', 'public.page.privacyDesc'], help: ['public.page.help', 'public.page.helpDesc'], status: ['public.page.status', 'public.page.statusDesc'], trust: ['public.page.trust', 'public.page.trustDesc'], pricing: ['public.page.pricing', 'public.page.pricingDesc'], docs: ['public.page.docs', 'public.page.docsDesc'] }; const [titleKey, descKey] = content[page]; return <div className="public-shell"><header className="public-topbar"><Link className="brand dark" to="/"><span className="brand-mark"><Sparkles size={16} /></span>{t('public.brand')}</Link><nav><Link to="/pricing">{t('public.nav.pricing')}</Link><Link to="/docs">{t('public.nav.docs')}</Link><Link to="/status">{t('public.nav.status')}</Link><Link to="/trust">{t('public.nav.trust')}</Link><Link className="button button-primary" to="/login">{t('public.nav.openConsole')}</Link></nav></header><main className="public-main"><span className="eyebrow">{t('public.eyebrow')}</span><h1>{t(titleKey)}</h1><p className="public-lead">{t(descKey)}</p>{page === 'pricing' ? <div className="public-plans"><div><strong>{t('public.pricing.pro')}</strong><span>{t('public.pricing.proPrice')}</span><p>{t('public.pricing.proDesc')}</p><Link className="button button-primary" to="/register">{t('public.pricing.startPro')} <ArrowUpRight size={15} /></Link></div><div className="featured"><small className="featured-badge">{t('public.pricing.recommended')}</small><strong>{t('public.pricing.scale')}</strong><span>{t('public.pricing.scalePrice')}</span><p>{t('public.pricing.scaleDesc')}</p><Link className="button button-primary" to="/help">{t('public.pricing.talkSales')} <ArrowUpRight size={15} /></Link></div><div><strong>{t('public.pricing.enterprise')}</strong><span>{t('public.pricing.enterprisePrice')}</span><p>{t('public.pricing.enterpriseDesc')}</p><Link className="button" to="/help">{t('public.pricing.contactTeam')} <ArrowUpRight size={15} /></Link></div></div> : page === 'docs' ? <div className="docs-layout"><aside><strong>{t('public.page.docs')}</strong><a className="active">{t('public.docs.gettingStarted')}</a><a>{t('public.docs.authentication')}</a><a>{t('public.docs.openaiCompat')}</a><a>{t('public.docs.wsSessions')}</a><a>{t('public.docs.webhooks')}</a></aside><article className="doc-article"><h2>{t('public.docs.buildTitle')}</h2><p>{t('public.docs.buildBody')}</p><pre>{'curl https://api.workama.example/api/v1/sessions -H "Authorization: Bearer $WORKAMA_TOKEN" -H "Content-Type: application/json" -d \'{"title":"Research brief","model":"workama-chat"}\''}</pre><div className="callout"><ShieldCheck size={16} /><span>{t('public.docs.idempotencyNote')}</span></div></article></div> : <div className="public-panel"><div className="public-panel-icon"><Globe2 size={20} /></div><h2>{t('public.builtForAccountable')}</h2><p>{t('public.exploreConsole')}</p><div className="public-links"><Link to="/login">{t('public.openConsole')} <ArrowUpRight size={15} /></Link><Link to="/docs">{t('public.readApiDocs')} <ArrowUpRight size={15} /></Link></div></div>}</main><footer><span>{t('public.copyright')}</span><span>{t('public.builtFor')}</span></footer></div> }
function RootPage() { const { authenticated, loading } = useAuth(); const { t } = useLocale(); if (loading) return <StateView state="loading" title={t('public.preparing')} />; if (authenticated) return <Navigate to="/chat" replace />; return <div className="public-shell"><header className="public-topbar"><Link className="brand dark" to="/"><span className="brand-mark"><Sparkles size={16} /></span>{t('public.brand')}</Link><nav><Link to="/pricing">{t('public.nav.pricing')}</Link><Link to="/docs">{t('public.nav.docs')}</Link><Link to="/status">{t('public.nav.status')}</Link><Link className="button button-primary" to="/login">{t('public.nav.openConsole')}</Link></nav></header><main className="landing-main"><div><span className="eyebrow">{t('public.eyebrowAi')}</span><h1>{t('public.tagline')}</h1><p>{t('public.taglineDescription')}</p><div className="landing-actions"><Link className="button button-primary" to="/login">{t('public.startOperating')} <ArrowUpRight size={16} /></Link><Link className="button" to="/docs">{t('public.readDocs')}</Link></div></div><div className="landing-signals"><div><strong>01</strong><span>{t('public.signal.context')}</span><small>{t('public.signal.contextDesc')}</small></div><div><strong>02</strong><span>{t('public.signal.action')}</span><small>{t('public.signal.actionDesc')}</small></div><div><strong>03</strong><span>{t('public.signal.evidence')}</span><small>{t('public.signal.evidenceDesc')}</small></div></div></main><footer><span>{t('public.copyrightSymbol')}</span><span>{t('public.builtForTeams')}</span></footer></div> }
const onboardingQuestions = [
  { id: 'user_role', titleKey: 'onboarding.q.userRole.title' as const, descKey: 'onboarding.q.userRole.desc' as const, options: [['individual', 'onboarding.q.userRole.individual' as const, 'onboarding.q.userRole.individualDesc' as const], ['product', 'onboarding.q.userRole.product' as const, 'onboarding.q.userRole.productDesc' as const], ['engineering', 'onboarding.q.userRole.engineering' as const, 'onboarding.q.userRole.engineeringDesc' as const], ['design', 'onboarding.q.userRole.design' as const, 'onboarding.q.userRole.designDesc' as const], ['manager', 'onboarding.q.userRole.manager' as const, 'onboarding.q.userRole.managerDesc' as const]] },
  { id: 'primary_goal', titleKey: 'onboarding.q.primaryGoal.title' as const, descKey: 'onboarding.q.primaryGoal.desc' as const, options: [['chat', 'onboarding.q.primaryGoal.chat' as const, 'onboarding.q.primaryGoal.chatDesc' as const], ['knowledge', 'onboarding.q.primaryGoal.knowledge' as const, 'onboarding.q.primaryGoal.knowledgeDesc' as const], ['gateway', 'onboarding.q.primaryGoal.gateway' as const, 'onboarding.q.primaryGoal.gatewayDesc' as const], ['work', 'onboarding.q.primaryGoal.work' as const, 'onboarding.q.primaryGoal.workDesc' as const], ['code', 'onboarding.q.primaryGoal.code' as const, 'onboarding.q.primaryGoal.codeDesc' as const]] },
  { id: 'team_size', titleKey: 'onboarding.q.teamSize.title' as const, descKey: 'onboarding.q.teamSize.desc' as const, options: [['1', 'onboarding.q.teamSize.1' as const, 'onboarding.q.teamSize.1Desc' as const], ['2-10', 'onboarding.q.teamSize.2-10' as const, 'onboarding.q.teamSize.2-10Desc' as const], ['11-50', 'onboarding.q.teamSize.11-50' as const, 'onboarding.q.teamSize.11-50Desc' as const], ['51-200', 'onboarding.q.teamSize.51-200' as const, 'onboarding.q.teamSize.51-200Desc' as const], ['201+', 'onboarding.q.teamSize.201+' as const, 'onboarding.q.teamSize.201+Desc' as const]] },
  { id: 'data_sensitivity', titleKey: 'onboarding.q.dataSensitivity.title' as const, descKey: 'onboarding.q.dataSensitivity.desc' as const, options: [['public', 'onboarding.q.dataSensitivity.public' as const, 'onboarding.q.dataSensitivity.publicDesc' as const], ['standard', 'onboarding.q.dataSensitivity.standard' as const, 'onboarding.q.dataSensitivity.standardDesc' as const], ['confidential', 'onboarding.q.dataSensitivity.confidential' as const, 'onboarding.q.dataSensitivity.confidentialDesc' as const], ['restricted', 'onboarding.q.dataSensitivity.restricted' as const, 'onboarding.q.dataSensitivity.restrictedDesc' as const], ['unknown', 'onboarding.q.dataSensitivity.unknown' as const, 'onboarding.q.dataSensitivity.unknownDesc' as const]] },
  { id: 'preferred_model', titleKey: 'onboarding.q.preferredModel.title' as const, descKey: 'onboarding.q.preferredModel.desc' as const, options: [['quality', 'onboarding.q.preferredModel.quality' as const, 'onboarding.q.preferredModel.qualityDesc' as const], ['speed', 'onboarding.q.preferredModel.speed' as const, 'onboarding.q.preferredModel.speedDesc' as const], ['cost', 'onboarding.q.preferredModel.cost' as const, 'onboarding.q.preferredModel.costDesc' as const], ['balanced', 'onboarding.q.preferredModel.balanced' as const, 'onboarding.q.preferredModel.balancedDesc' as const], ['workama-chat', 'onboarding.q.preferredModel.workama-chat' as const, 'onboarding.q.preferredModel.workama-chatDesc' as const]] },
  { id: 'notification_preference', titleKey: 'onboarding.q.notifications.title' as const, descKey: 'onboarding.q.notifications.desc' as const, options: [['in_app', 'onboarding.q.notifications.in_app' as const, 'onboarding.q.notifications.in_appDesc' as const], ['in_app_email', 'onboarding.q.notifications.in_app_email' as const, 'onboarding.q.notifications.in_app_emailDesc' as const], ['approvals', 'onboarding.q.notifications.approvals' as const, 'onboarding.q.notifications.approvalsDesc' as const], ['usage', 'onboarding.q.notifications.usage' as const, 'onboarding.q.notifications.usageDesc' as const], ['all', 'onboarding.q.notifications.all' as const, 'onboarding.q.notifications.allDesc' as const]] },
] as const

function OnboardingPage() {
  const { refreshUser } = useAuth(); const navigate = useNavigate(); const { t } = useLocale()
  const [step, setStep] = useState(0); const [answers, setAnswers] = useState<Record<string, string>>(() => { try { return JSON.parse(sessionStorage.getItem('workama_onboarding_draft') ?? '{}') as Record<string, string> } catch { return {} } })
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const question = onboardingQuestions[step]; const selected = answers[question.id]
  useEffect(() => { sessionStorage.setItem('workama_onboarding_draft', JSON.stringify(answers)) }, [answers])
  function choose(value: string) { setAnswers((current) => ({ ...current, [question.id]: value })); setError('') }
  async function complete() { setBusy(true); setError(''); try { await api.post('/api/v1/auth/onboarding', { user_role: answers.user_role ?? 'individual', primary_goal: answers.primary_goal ?? 'chat', team_size: answers.team_size ?? '1', data_sensitivity: answers.data_sensitivity ?? 'standard', preferred_model: answers.preferred_model ?? 'workama-chat', notification_preference: answers.notification_preference ?? 'in_app' }); sessionStorage.removeItem('workama_onboarding_draft'); await refreshUser(); navigate('/chat') } catch (caught) { setError(caught instanceof Error ? caught.message : t('onboarding.saveError')) } finally { setBusy(false) } }
  function next() { if (!selected) { setError(t('onboarding.choosePrompt')); return } if (step === onboardingQuestions.length - 1) { void complete(); return } setStep((value) => value + 1) }
  return <div className="onboarding-shell"><div className="onboarding-card"><div className="auth-mobile-brand"><Sparkles size={18} />{t('public.brand')}</div><div className="onboarding-progress"><span>{t('onboarding.questionLabel')} {String(step + 1).padStart(2, '0')} / {String(onboardingQuestions.length).padStart(2, '0')}</span><div aria-hidden="true"><i style={{ width: `${((step + 1) / onboardingQuestions.length) * 100}%` }} /></div></div><span className="eyebrow">{t('onboarding.eyebrow')}</span><h1>{t(question.titleKey)}</h1><p>{t(question.descKey)}</p><div className="onboarding-options">{question.options.map(([value, titleKey, descKey]) => <button key={value} type="button" className={`onboarding-option ${selected === value ? 'selected' : ''}`} onClick={() => choose(value)}><span>{String(question.options.findIndex((item) => item[0] === value) + 1).padStart(2, '0')}</span><div><strong>{t(titleKey)}</strong><small>{t(descKey)}</small></div>{selected === value && <Check size={17} />}</button>)}</div>{error && <div className="alert alert-error">{error}</div>}<div className="onboarding-actions"><Button variant="ghost" disabled={step === 0 || busy} onClick={() => setStep((value) => Math.max(0, value - 1))}>{t('onboarding.back')}</Button><Button variant="ghost" disabled={busy} onClick={() => { if (step === onboardingQuestions.length - 1) { void complete() } else { setStep((value) => value + 1) } }}>{t('onboarding.skip')}</Button><Button variant="primary" loading={busy} onClick={next}>{step === onboardingQuestions.length - 1 ? t('onboarding.complete') : t('onboarding.continue')} <ArrowUpRight size={16} /></Button></div></div></div>
}
function SetupPage() { const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading'); const { t } = useLocale(); useEffect(() => { void api.get<{ initialized: boolean }>('/api/v1/setup/status').then(() => setStatus('ready')).catch(() => setStatus('error')) }, []); return <div className="auth-shell simple"><main className="auth-main"><div className="auth-panel"><span className="eyebrow">{t('setup.eyebrow')}</span><h1>{t('setup.title')}</h1>{status === 'loading' && <StateView state="loading" />}{status === 'error' && <StateView state="error" description={t('setup.unavailable')} onRetry={() => window.location.reload()} />}{status === 'ready' && <><p>{t('setup.ready')}</p><Link className="button button-primary" to="/login">{t('setup.continue')} <ArrowUpRight size={16} /></Link></>}</div></main></div> }
function ArtifactPage() { const location = useLocation(); const { t } = useLocale(); const token = location.pathname.split('/').pop(); const [data, setData] = useState<Record<string, unknown> | null>(null); useEffect(() => { if (token) void api.get<Record<string, unknown>>(`/api/v1/public/artifacts/${encodeURIComponent(token)}`).then(setData).catch(() => setData(null)) }, [token]); return <div className="public-shell"><header className="public-topbar"><Link className="brand dark" to="/login"><span className="brand-mark"><Sparkles size={16} /></span>{t('public.brand')}</Link><span className="trace-pill">{t('artifact.shared')}</span></header><main className="artifact-main">{data ? <><span className="eyebrow">{t('artifact.eyebrow')}</span><h1>{String(data.name ?? t('artifact.nameFallback'))}</h1><div className="artifact-meta"><span>{String(data.content_type ?? 'text/markdown')}</span><span>{String(data.provenance_status ?? 'verified')}</span></div><pre>{String(data.preview ?? data.content ?? t('artifact.noPreview'))}</pre><div className="callout"><LockKeyhole size={16} /><span>{t('artifact.scopedNote')}</span></div></> : <StateView state="error" title={t('artifact.unavailable')} description={t('artifact.unavailableDesc')} />}</main></div> }
function PublishedAppPage() { const { appId = '' } = useParams(); const { t } = useLocale(); const [data, setData] = useState<Record<string, unknown> | null>(null); const [loading, setLoading] = useState(true); useEffect(() => { void api.get<Record<string, unknown>>(`/api/v1/public/assistants/${encodeURIComponent(appId)}`).then(setData).catch(() => setData(null)).finally(() => setLoading(false)) }, [appId]); const loginPath = `/login?redirect=${encodeURIComponent(`/chat?assistant=${appId}`)}`; return <PublicFrame><main className="artifact-main">{loading ? <StateView state="loading" /> : data ? <><span className="eyebrow">{t('app.published.eyebrow')}</span><h1>{String(data.name ?? t('app.published.nameFallback'))}</h1><p className="public-lead">{String(data.description ?? t('app.published.descFallback'))}</p><div className="public-panel"><h2>{t('app.published.readyTitle')}</h2><p>{String(data.greeting ?? t('app.published.greetingFallback'))}</p><Link className="button button-primary" to={loginPath}>{t('app.published.signInToUse')} <ArrowUpRight size={16} /></Link></div></> : <StateView state="error" title={t('app.published.unavailable')} description={t('app.published.unavailableDesc')} />}</main></PublicFrame> }
function OAuthCallbackPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const { t } = useLocale()
  const query = new URLSearchParams(location.search)
  const provider = query.get('provider') ?? 'google'
  const code = query.get('code')
  const oauthState = query.get('state')
  const [status, setStatus] = useState<'loading' | 'success' | 'pending' | 'error'>('loading')
  const [message, setMessage] = useState(t('oauth.validating'))

  useEffect(() => {
    if (!code || !oauthState) {
      setStatus('error')
      setMessage(t('oauth.incomplete'))
      return
    }
    void api.get(`/api/v1/auth/oauth/${encodeURIComponent(provider)}/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(oauthState)}`).then(() => {
      setStatus('success')
      setMessage(t('oauth.verified'))
      window.setTimeout(() => navigate('/chat', { replace: true }), 700)
    }).catch((caught) => {
      const responseStatus = caught && typeof caught === 'object' && 'status' in caught ? Number(caught.status) : 0
      if (responseStatus === 403) {
        setStatus('error')
        setMessage(t('oauth.notProvisioned'))
      } else {
        setStatus('error')
        setMessage(caught instanceof Error ? caught.message : t('oauth.validateError'))
      }
    })
  }, [code, navigate, oauthState, provider, t])

  const title = status === 'loading' ? t('oauth.titleLoading') : status === 'success' ? t('oauth.titleSuccess') : status === 'pending' ? t('oauth.titlePending') : t('oauth.titleError')
  return <div className="auth-shell simple"><main className="auth-main"><div className="auth-panel"><span className="eyebrow">{t('oauth.eyebrow')}</span><h1>{title}</h1><p>{message}</p>{status === 'error' && <div className="alert alert-error">{t('oauth.checkProvider')}</div>}{status !== 'success' && <Link className="button button-primary" to="/login">{t('oauth.returnToSignIn')} <ArrowUpRight size={16} /></Link>}</div></main></div>
}
function InvitationPage() { const { token = '' } = useParams(); const { authenticated } = useAuth(); const { t } = useLocale(); const [inviteToken, setInviteToken] = useState(token); const [notice, setNotice] = useState(''); const [error, setError] = useState(''); async function accept() { try { await api.post(`/api/v1/invitations/${encodeURIComponent(token)}/accept`, { token: inviteToken }); setNotice(t('invitation.accepted')); setError('') } catch (caught) { setError(caught instanceof Error ? caught.message : t('invitation.acceptError')) } } return <div className="auth-shell simple"><main className="auth-main"><div className="auth-panel"><span className="eyebrow">{t('invitation.eyebrow')}</span><h1>{t('invitation.title')}</h1><p>{t('invitation.desc')}</p>{error && <div className="alert alert-error">{error}</div>}{notice && <div className="alert alert-info">{notice}</div>}{authenticated ? <form className="form-stack" onSubmit={(event) => { event.preventDefault(); void accept() }}><Field label={t('invitation.tokenLabel')}><input value={inviteToken} onChange={(event) => setInviteToken(event.target.value)} required /></Field><Button type="submit" variant="primary">{t('invitation.accept')} <ArrowUpRight size={16} /></Button></form> : <Link className="button button-primary" to={`/login?redirect=${encodeURIComponent(`/invitations/${token}`)}`}>{t('invitation.signInToContinue')} <ArrowUpRight size={16} /></Link>}</div></main></div> }
function NotFound() { const { t } = useLocale(); return <div className="auth-shell simple"><main className="auth-main"><div className="auth-panel"><span className="eyebrow">{t('notFound.eyebrow')}</span><h1>{t('notFound.title')}</h1><p>{t('notFound.desc')}</p><Link className="button button-primary" to="/chat">{t('notFound.backToConsole')} <ArrowLeft size={16} /></Link></div></main></div> }
function PublicFreeProvidersPage() { return <PublicFrame><main className="public-main public-free-providers-main"><FreeProvidersPage readOnly /></main></PublicFrame> }
function SuspendedOutlet() { return <Suspense fallback={<div className="route-loading">Loading…</div>}><Outlet /></Suspense> }

export default function App() {
  return <Routes>
    <Route path="/" element={<RootPage />} />
    <Route path="/login" element={<AuthPage mode="login" />} />
    <Route path="/register" element={<AuthPage mode="register" />} />
    <Route path="/oauth/callback" element={<OAuthCallbackPage />} />
    <Route path="/invitations/:token" element={<InvitationPage />} />
    <Route path="/app/:appId" element={<PublishedAppPage />} />
    <Route path="/verify-email" element={<UtilityPage mode="verify-email" />} />
    <Route path="/forgot-password" element={<UtilityPage mode="forgot-password" />} />
    <Route path="/reset-password" element={<UtilityPage mode="reset-password" />} />
    <Route path="/mfa/challenge" element={<UtilityPage mode="mfa" />} />
    <Route path="/setup" element={<SetupPage />} />
    <Route path="/free-providers" element={<PublicFreeProvidersPage />} />
    <Route path="/developers" element={<DevelopersPage />} />
    {(['terms', 'privacy', 'help', 'status', 'trust', 'pricing', 'docs'] as const).map((page) => <Route key={page} path={`/${page}`} element={<PublicPage page={page} />} />)},
    <Route path="/artifact/:token" element={<ArtifactPage />} />
    <Route element={<RequireAuth />}>
      <Route path="/onboarding" element={<OnboardingPage />} />
      <Route element={<ConsoleLayout />}>
        <Route element={<SuspendedOutlet />}>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="/knowledge/:datasetId" element={<KnowledgePage />} />
        <Route path="/datasets" element={<KnowledgePage />} />
        <Route path="/datasets/:datasetId" element={<KnowledgePage />} />
        <Route path="/datasets/:datasetId/test" element={<KnowledgePage />} />
        <Route path="/datasets/:datasetId/evals" element={<RagEvaluationPage />} />
        <Route path="/knowledge/evaluation" element={<RagEvaluationPage />} />
        <Route path="/knowledge/evaluation/:evalSetId" element={<RagEvaluationDetailPage />} />
        <Route path="/knowledge/:datasetId/evals" element={<RagEvaluationPage />} />
        <Route path="/workflows" element={<WorkflowPage />} />
        <Route path="/ama-design" element={<DesignPage />} />
        <Route path="/design" element={<DesignPage />} />
        <Route path="/studio/apps" element={<AppStudioPage />} />
        <Route path="/studio/apps/:appId" element={<AppStudioDetailPage />} />
        <Route path="/studio/apps/:appId/editor" element={<AppStudioDetailPage editor />} />
        <Route path="/studio/apps/:appId/runs" element={<AppStudioRunsPage />} />
        <Route path="/studio/integrations" element={<StudioIntegrationsPage />} />
        <Route path="/studio/marketplace" element={<MarketplacePage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/automations" element={<AutomationsPage />} />
        <Route path="/agents/:sessionId" element={<AgentDetailPage />} />
        <Route path="/agents/tools" element={<AgentToolsPage />} />
        <Route path="/agents/code" element={<CodePage />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/gateway/channels" element={<GatewayConsolePage section="channels" />} />
        <Route path="/gateway/tokens" element={<GatewayConsolePage section="tokens" />} />
        <Route path="/gateway/usage" element={<GatewayConsolePage section="usage" />} />
        <Route path="/gateway/logs" element={<GatewayConsolePage section="logs" />} />
        <Route path="/gateway/pricing" element={<GatewayConsolePage section="pricing" />} />
        <Route path="/gateway/import-diagnostics" element={<GatewayImportDiagnosticsPage />} />
        <Route path="/admin/operations" element={<OperationsPage />} />
        <Route path="/admin/platform-operations" element={<OperationsPage />} />
        <Route path="/admin/audit" element={<AuditPage />} />
        <Route path="/admin/security" element={<SecurityPage />} />
        <Route path="/admin/settings" element={<WorkspaceSettingsPage />} />
        <Route path="/settings" element={<WorkspaceSettingsPage />} />
        <Route path="/admin/members" element={<MembersPage />} />
        <Route path="/admin/api-keys" element={<ApiKeysPage />} />
        <Route path="/admin/tool-approvals" element={<ToolApprovalsPage />} />
        <Route path="/admin/observability" element={<ObservabilityPage />} />
        <Route path="/admin/integrations" element={<StudioIntegrationsPage />} />
        <Route path="/admin/platform-support" element={<PlatformSupportPage />} />
        <Route path="/admin/privacy" element={<PrivacyPage />} />
        <Route path="/admin/enterprise-identity" element={<EnterpriseIdentityPage />} />
        <Route path="/admin/compliance" element={<CompliancePage />} />
        <Route path="/gateway/free-providers" element={<FreeProvidersPage />} />
        <Route path="/memory" element={<MemoryPage />} />
        <Route path="/work" element={<WorkPage />} />
        </Route>
      </Route>
            <Route element={<AdminLayout />}>
        <Route element={<SuspendedOutlet />}>
          <Route path="/admin" element={<AdminDashboardPage />} />
          <Route path="/admin/workspaces" element={<AdminWorkspacesPage />} />
          <Route path="/admin/assistants" element={<AdminAssistantsPage />} />
          <Route path="/admin/workflows" element={<AdminWorkflowsPage />} />
          <Route path="/admin/knowledge-bases" element={<AdminKnowledgeBasesPage />} />
          <Route path="/admin/devices" element={<DevicesPage />} />
          <Route path="/admin/billing" element={<AdminBillingPage />} />
          <Route path="/admin/audit-logs" element={<AdminAuditLogsPage />} />
          <Route path="/admin/mcp-tools" element={<AdminMcpToolsPage />} />
          <Route path="/admin/notifications" element={<NotificationsPage />} />
          <Route path="/admin/files" element={<AdminFilesPage />} />
          <Route path="/admin/memory-vectors" element={<AdminMemoryVectorsPage />} />
          <Route path="/admin/free-providers" element={<FreeProvidersPage />} />
          <Route path="/admin/connectors" element={<AdminConnectorsPage />} />
          <Route path="/admin/automations" element={<AdminAutomationsPage />} />
          <Route path="/admin/push" element={<AdminPushPage />} />
          <Route path="/admin/design-projects" element={<AdminDesignProjectsPage />} />
          <Route path="/admin/external-apps" element={<AdminExternalAppsPage />} />
          <Route path="/admin/agent-planner" element={<AdminAgentPlannerPage />} />
        </Route>
      </Route>
    </Route>
    <Route path="*" element={<NotFound />} />
  </Routes>
}
