// A recommendation or saved draft does not mean a human has received the case.
export function chatStatus(response = {}) {
  const pending = response.tasks?.some(task => task.status === 'waiting_confirmation')
  if (pending) return { label: '待确认工单', detail: '回答生成时，工单草稿尚未提交。请到工单页核对、确认，并查看最新处理状态。', tone: 'pending' }
  if (response.escalated || response.handoffStatus === 'recommended') {
    const reasons = response.escalationReasons?.filter(Boolean).join('；')
    return { label: '建议人工核验', detail: `${reasons ? reasons + '。' : ''}当前尚未转交人工，可通过工单继续处理。`, tone: 'pending' }
  }
  return { label: 'AI 答复', detail: '', tone: 'normal' }
}

const roles = { general: '通用客服', technical: '技术支持', billing: '账单服务', escalation: '人工核验' }
export function replyMeta(response, admin = false) {
  const status = chatStatus(response).label
  return admin ? [roles[response.primaryAgent || response.agentType] || '客服助手', status].join(' · ') : status
}
