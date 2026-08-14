import { describe, expect, it } from 'vitest'
import { getInitialLocale, translate, messages } from './index'

describe('shared locale messages', () => {
  it('has identical keys in zh-CN and en-US (no drift)', () => {
    const zhKeys = Object.keys(messages['zh-CN']).sort()
    const enKeys = Object.keys(messages['en-US']).sort()
    const zhOnly = zhKeys.filter(k => !enKeys.includes(k))
    const enOnly = enKeys.filter(k => !zhKeys.includes(k))
    expect(zhOnly).toEqual([])
    expect(enOnly).toEqual([])
    expect(zhKeys.length).toBe(enKeys.length)
  })
  it('keeps the Chinese navigation copy readable', () => {
    expect(translate('zh-CN', 'nav.knowledge')).toBe('知识库')
    expect(translate('zh-CN', 'ui.signOut')).toBe('退出登录')
  })

  it('keeps billing business fields localized in both supported locales', () => {
    expect(translate('zh-CN', 'billing.creditGrantBuckets')).toBe('赠送积分批次')
    expect(translate('zh-CN', 'billing.status.expired')).toBe('已过期')
    expect(translate('en-US', 'billing.quota.granted_credits_month')).toBe('Credits this month')
    expect(translate('en-US', 'billing.status.succeeded')).toBe('Succeeded')
  })

  it('keeps the Chat command center business copy localized', () => {
    expect(translate('zh-CN', 'chat.startWithPrompt')).toBe('从一个提示开始')
    expect(translate('zh-CN', 'chat.prompt.decide.risk')).toBe('总结当前项目风险。')
    expect(translate('en-US', 'chat.workspacePulse')).toBe('Workspace pulse')
  })

  it('keeps notification review controls localized', () => {
    expect(translate('zh-CN', 'notifications.inbox')).toBe('通知收件箱')
    expect(translate('zh-CN', 'notifications.priority.high')).toBe('高优先级')
    expect(translate('en-US', 'notifications.delivery')).toBe('Delivery history')
    expect(translate('en-US', 'notifications.status.pending_external')).toBe('External delivery pending')
  })

  it('keeps security and privacy controls localized', () => {
    expect(translate('zh-CN', 'security.identityControls')).toBe('身份控制')
    expect(translate('zh-CN', 'privacy.dataRequests')).toBe('数据请求')
    expect(translate('en-US', 'security.confirmEnable')).toBe('Confirm and enable')
    expect(translate('en-US', 'privacy.requestSubmitted')).toBe('Privacy request submitted and queued for auditable processing.')
    expect(translate('en-US', 'governance.status.revoked')).toBe('Revoked')
    expect(translate('zh-CN', 'devices.boundary')).toBe('设备控制边界')
    expect(translate('en-US', 'devices.revokePasskeyNotice')).toBe('Passkey revoked.')
    expect(translate('zh-CN', 'enterprise.activationGuardrail')).toBe('启用前检查')
    expect(translate('en-US', 'enterprise.rotateScimNotice')).toBe('SCIM token rotated. Copy the new value before closing this dialog.')
    expect(translate('zh-CN', 'devices.showingRecent')).toBe('仅显示最近 50 个会话；上方指标覆盖全部会话。')
    expect(translate('zh-CN', 'compliance.residency')).toBe('数据驻留')
    expect(translate('en-US', 'compliance.createJitGrant')).toBe('Create JIT grant')
    expect(translate('en-US', 'compliance.severityCritical')).toBe('Critical')
    expect(translate('zh-CN', 'audit.ledger')).toBe('审计账本')
    expect(translate('en-US', 'observability.semanticContract')).toBe('Telemetry semantic contract')
    expect(translate('zh-CN', 'governance.status.no_data')).toBe('无数据')
  })

  it('normalizes browser language preferences', () => {
    expect(getInitialLocale('zh-Hans-CN')).toBe('zh-CN')
    expect(getInitialLocale('en-US')).toBe('en-US')
    expect(getInitialLocale(null)).toBe('en-US')
  })

  it('keeps common actions localized in both locales', () => {
    expect(translate('zh-CN', 'common.actions.save')).toBe('保存')
    expect(translate('zh-CN', 'common.actions.cancel')).toBe('取消')
    expect(translate('zh-CN', 'common.actions.delete')).toBe('删除')
    expect(translate('zh-CN', 'common.actions.confirm')).toBe('确认')
    expect(translate('zh-CN', 'common.actions.submit')).toBe('提交')
    expect(translate('zh-CN', 'common.actions.back')).toBe('返回')
    expect(translate('zh-CN', 'common.actions.next')).toBe('下一步')
    expect(translate('en-US', 'common.actions.save')).toBe('Save')
    expect(translate('en-US', 'common.actions.cancel')).toBe('Cancel')
    expect(translate('en-US', 'common.actions.delete')).toBe('Delete')
    expect(translate('en-US', 'common.actions.confirm')).toBe('Confirm')
    expect(translate('en-US', 'common.actions.submit')).toBe('Submit')
    expect(translate('en-US', 'common.actions.back')).toBe('Back')
    expect(translate('en-US', 'common.actions.next')).toBe('Next')
  })

  it('keeps status labels localized in both locales', () => {
    expect(translate('zh-CN', 'common.status.active')).toBe('活跃')
    expect(translate('zh-CN', 'common.status.inactive')).toBe('未激活')
    expect(translate('zh-CN', 'common.status.pending')).toBe('待处理')
    expect(translate('zh-CN', 'common.status.suspended')).toBe('已暂停')
    expect(translate('zh-CN', 'common.status.deleted')).toBe('已删除')
    expect(translate('en-US', 'common.status.active')).toBe('Active')
    expect(translate('en-US', 'common.status.inactive')).toBe('Inactive')
    expect(translate('en-US', 'common.status.pending')).toBe('Pending')
    expect(translate('en-US', 'common.status.suspended')).toBe('Suspended')
    expect(translate('en-US', 'common.status.deleted')).toBe('Deleted')
  })

  it('keeps HTTP error messages localized in both locales', () => {
    expect(translate('zh-CN', 'errors.http.unauthorized')).toBe('未授权访问，请登录后重试。')
    expect(translate('zh-CN', 'errors.http.forbidden')).toBe('您没有权限执行此操作。')
    expect(translate('zh-CN', 'errors.http.notFound')).toBe('请求的资源不存在。')
    expect(translate('zh-CN', 'errors.http.internalError')).toBe('服务器内部错误，请稍后重试。')
    expect(translate('en-US', 'errors.http.unauthorized')).toBe('Unauthorized access. Please sign in and try again.')
    expect(translate('en-US', 'errors.http.forbidden')).toBe('You do not have permission to perform this action.')
    expect(translate('en-US', 'errors.http.notFound')).toBe('The requested resource does not exist.')
    expect(translate('en-US', 'errors.http.internalError')).toBe('Internal server error. Please try again later.')
  })

  it('keeps validation messages localized in both locales', () => {
    expect(translate('zh-CN', 'validation.required')).toBe('此字段为必填项。')
    expect(translate('zh-CN', 'validation.email')).toBe('请输入有效的邮箱地址。')
    expect(translate('zh-CN', 'validation.minLength')).toBe('至少需要 {min} 个字符。')
    expect(translate('zh-CN', 'validation.maxLength')).toBe('最多允许 {max} 个字符。')
    expect(translate('en-US', 'validation.required')).toBe('This field is required.')
    expect(translate('en-US', 'validation.email')).toBe('Please enter a valid email address.')
    expect(translate('en-US', 'validation.minLength')).toBe('At least {min} characters required.')
    expect(translate('en-US', 'validation.maxLength')).toBe('Maximum {max} characters allowed.')
  })

  it('keeps chat session actions localized in both locales', () => {
    expect(translate('zh-CN', 'chat.session.retry')).toBe('重试')
    expect(translate('zh-CN', 'chat.session.copy')).toBe('复制')
    expect(translate('zh-CN', 'chat.session.edit')).toBe('编辑')
    expect(translate('zh-CN', 'chat.session.delete')).toBe('删除')
    expect(translate('zh-CN', 'chat.session.share')).toBe('分享')
    expect(translate('zh-CN', 'chat.session.export')).toBe('导出')
    expect(translate('en-US', 'chat.session.retry')).toBe('Retry')
    expect(translate('en-US', 'chat.session.copy')).toBe('Copy')
    expect(translate('en-US', 'chat.session.edit')).toBe('Edit')
    expect(translate('en-US', 'chat.session.delete')).toBe('Delete')
    expect(translate('en-US', 'chat.session.share')).toBe('Share')
    expect(translate('en-US', 'chat.session.export')).toBe('Export')
  })

  it('keeps chat artifact labels localized in both locales', () => {
    expect(translate('zh-CN', 'chat.artifact.title')).toBe('工件')
    expect(translate('zh-CN', 'chat.artifact.create')).toBe('创建工件')
    expect(translate('zh-CN', 'chat.artifact.version')).toBe('版本')
    expect(translate('zh-CN', 'chat.artifact.download')).toBe('下载')
    expect(translate('zh-CN', 'chat.artifact.preview')).toBe('预览')
    expect(translate('en-US', 'chat.artifact.title')).toBe('Artifact')
    expect(translate('en-US', 'chat.artifact.create')).toBe('Create artifact')
    expect(translate('en-US', 'chat.artifact.version')).toBe('Version')
    expect(translate('en-US', 'chat.artifact.download')).toBe('Download')
    expect(translate('en-US', 'chat.artifact.preview')).toBe('Preview')
  })

  it('keeps chat thread labels localized in both locales', () => {
    expect(translate('zh-CN', 'chat.thread.title')).toBe('对话线程')
    expect(translate('zh-CN', 'chat.thread.newThread')).toBe('新建线程')
    expect(translate('zh-CN', 'chat.thread.parentThread')).toBe('父线程')
    expect(translate('en-US', 'chat.thread.title')).toBe('Conversation thread')
    expect(translate('en-US', 'chat.thread.newThread')).toBe('New thread')
    expect(translate('en-US', 'chat.thread.parentThread')).toBe('Parent thread')
  })

  it('keeps chat message roles and statuses localized in both locales', () => {
    expect(translate('zh-CN', 'chat.message.role.user')).toBe('用户')
    expect(translate('zh-CN', 'chat.message.role.assistant')).toBe('助手')
    expect(translate('zh-CN', 'chat.message.status.sending')).toBe('发送中')
    expect(translate('zh-CN', 'chat.message.status.sent')).toBe('已发送')
    expect(translate('zh-CN', 'chat.message.status.delivered')).toBe('已送达')
    expect(translate('en-US', 'chat.message.role.user')).toBe('User')
    expect(translate('en-US', 'chat.message.role.assistant')).toBe('Assistant')
    expect(translate('en-US', 'chat.message.status.sending')).toBe('Sending')
    expect(translate('en-US', 'chat.message.status.sent')).toBe('Sent')
    expect(translate('en-US', 'chat.message.status.delivered')).toBe('Delivered')
  })

  it('keeps chat history labels localized in both locales', () => {
    expect(translate('zh-CN', 'chat.history.title')).toBe('对话历史')
    expect(translate('zh-CN', 'chat.history.empty')).toBe('暂无历史')
    expect(translate('zh-CN', 'chat.history.today')).toBe('今天')
    expect(translate('zh-CN', 'chat.history.yesterday')).toBe('昨天')
    expect(translate('en-US', 'chat.history.title')).toBe('Conversation history')
    expect(translate('en-US', 'chat.history.empty')).toBe('No history yet')
    expect(translate('en-US', 'chat.history.today')).toBe('Today')
    expect(translate('en-US', 'chat.history.yesterday')).toBe('Yesterday')
  })

  it('keeps chat export labels localized in both locales', () => {
    expect(translate('zh-CN', 'chat.export.title')).toBe('导出对话')
    expect(translate('zh-CN', 'chat.export.format')).toBe('导出格式')
    expect(translate('zh-CN', 'chat.export.success')).toBe('对话已导出。')
    expect(translate('zh-CN', 'chat.export.failed')).toBe('对话导出失败。')
    expect(translate('en-US', 'chat.export.title')).toBe('Export conversation')
    expect(translate('en-US', 'chat.export.format')).toBe('Export format')
    expect(translate('en-US', 'chat.export.success')).toBe('Conversation exported.')
    expect(translate('en-US', 'chat.export.failed')).toBe('Conversation export failed.')
  })

  it('keeps chat share labels localized in both locales', () => {
    expect(translate('zh-CN', 'chat.share.title')).toBe('分享对话')
    expect(translate('zh-CN', 'chat.share.link')).toBe('分享链接')
    expect(translate('zh-CN', 'chat.share.copyLink')).toBe('复制链接')
    expect(translate('zh-CN', 'chat.share.linkGenerated')).toBe('分享链接已生成。')
    expect(translate('en-US', 'chat.share.title')).toBe('Share conversation')
    expect(translate('en-US', 'chat.share.link')).toBe('Share link')
    expect(translate('en-US', 'chat.share.copyLink')).toBe('Copy link')
    expect(translate('en-US', 'chat.share.linkGenerated')).toBe('Share link generated.')
  })

  it('ensures all new keys have non-empty values in both locales', () => {
    const zhMessages = messages['zh-CN']
    const enMessages = messages['en-US']
    
    const emptyZhKeys = Object.entries(zhMessages)
      .filter(([_, value]) => value === '')
      .map(([key]) => key)
    const emptyEnKeys = Object.entries(enMessages)
      .filter(([_, value]) => value === '')
      .map(([key]) => key)
    
    expect(emptyZhKeys).toEqual([])
    expect(emptyEnKeys).toEqual([])
  })
})
