import { backendMeta } from './backends.js'

export const decisionLabels = { accepted: '直接采纳', edited: '修改后采纳', discarded: '弃用' }
export const reasonLabels = { none: '无', incorrect: '事实或政策有误', incomplete: '回答不完整', irrelevant: '答非所问', outdated: '资料过期', missing_knowledge: '缺少知识', unsafe: '不当建议', other: '其他' }
export const outcomeLabels = { unknown: '尚未确认', resolved: '已解决', unresolved: '未解决' }

async function call(settings, path, method = 'GET', body, signal) {
  const response = await fetch(backendMeta('python', settings).baseUrl + path, {
    method, credentials: 'include', signal,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  let data
  try { data = await response.json() } catch { throw new Error(`服务返回异常（${response.status}）`) }
  if (!response.ok) {
    const detail = data.detail?.message || data.detail || '请求失败'
    throw Object.assign(new Error(typeof detail === 'string' ? detail : JSON.stringify(detail)), { status: response.status, code: data.detail?.code })
  }
  return data
}

const paging = ({ limit = 20, offset = 0 } = {}) => new URLSearchParams({ limit, offset }).toString()
export const fetchAnswers = (settings, options = {}) => call(settings, '/answers?' + paging(options), 'GET', undefined, options.signal)
export const fetchAnswer = (settings, id, signal) => call(settings, '/answers/' + encodeURIComponent(id), 'GET', undefined, signal)
export const saveFeedback = (settings, id, payload, signal) => call(settings, `/answers/${encodeURIComponent(id)}/feedback`, 'PUT', payload, signal)
export const fetchDiagnostics = (settings, options = {}) => call(settings, '/diagnostics/reports?' + paging(options), 'GET', undefined, options.signal)
export const fetchDiagnostic = (settings, id, signal) => call(settings, '/diagnostics/reports/' + encodeURIComponent(id), 'GET', undefined, signal)
export const createDiagnostic = (settings, days, signal) => call(settings, '/diagnostics/reports', 'POST', { days }, signal)

export function safeSourceUrl(value) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

export function metricText(metric) {
  return metric?.denominator ? `${(metric.value * 100).toFixed(1)}%（${metric.numerator}/${metric.denominator}）` : '暂无有效样本（0/0）'
}

export function feedbackPayload(form, revision, original) {
  const value = { revision, decision: form.decision, reason: form.decision === 'accepted' ? 'none' : form.reason,
    outcome: form.outcome || 'unknown', comment: form.comment.trim(), final_answer: form.decision === 'edited' ? form.final_answer.trim() : '' }
  if (!decisionLabels[value.decision]) throw new Error('请选择采纳方式')
  if (value.decision !== 'accepted' && (!reasonLabels[value.reason] || value.reason === 'none')) throw new Error('请选择修改或弃用的原因')
  if (value.reason === 'other' && !value.comment) throw new Error('请填写具体原因')
  if (value.decision === 'edited' && (!value.final_answer || value.final_answer === original.trim())) throw new Error('请填写实际修改后的回复')
  return value
}

export function diagnosticMarkdown(report) {
  const text = value => String(value ?? '').replace(/[\\`*_{}[\]<>]/g, '\\$&').replace(/\r?\n/g, ' ')
  const date = value => new Date(value * 1000).toISOString()
  return [
    '# EchoMind 客服诊断报告', '', `报告编号：${report.id}`,
    `咨询时间窗：${date(report.window.start_at)} 至 ${date(report.window.end_at)}，左闭右开。`,
    `反馈快照：${date(report.feedback_as_of)}`, '',
    `已保存回答 ${report.counts.answers} 条，已反馈 ${report.counts.reviewed} 条，未反馈 ${report.counts.unreviewed} 条。`,
    `反馈覆盖率：${metricText(report.metrics.review_coverage)}`,
    `直接采纳率：${metricText(report.metrics.acceptance_rate)}`,
    `修改采纳率：${metricText(report.metrics.edit_rate)}`,
    `弃用率：${metricText(report.metrics.discard_rate)}`,
    `已知结果中的解决率：${metricText(report.metrics.resolution_rate)}`,
    `解决结果未知：${report.counts.outcome_unknown} 条。`, '', '## 待处理问题',
    ...report.issue_examples.map(item => `- ${text(item.question)}；${reasonLabels[item.reason] || '结果未解决'}；请求 ${item.request_id}，反馈版本 ${item.feedback_revision}`),
    '', '## 改进建议', ...report.recommendations.map(item => '- ' + text(item)),
    '', '## 统计边界', ...report.limitations.map(item => '- ' + text(item)), ''
  ].join('\n')
}
