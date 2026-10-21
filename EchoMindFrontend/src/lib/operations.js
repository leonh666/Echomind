import { backendMeta } from './backends.js'

async function call(settings, path, method = 'GET', body) {
  const response = await fetch(backendMeta('python', settings).baseUrl + path, {
    method, credentials: 'include',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  const text = await response.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { data = { message: `服务返回异常（${response.status}）` } }
  if (!response.ok) {
    const detail = data.detail?.message || data.detail || data.message || '操作失败'
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    error.status = response.status
    error.code = data.detail?.code
    throw error
  }
  return data
}

export const sessionInfo = (settings) => call(settings, '/auth/me')
export const login = (settings, email, password) => call(settings, '/auth/login', 'POST', { email, password })
export const logout = (settings) => call(settings, '/auth/logout', 'POST', {})
export const tasks = (settings) => call(settings, '/tasks')
export const tickets = (settings) => call(settings, '/tickets')
export const createDraft = (settings, payload, key) => call(settings, '/tasks', 'POST', { payload, idempotency_key: key })
export const reviseDraft = (settings, task, payload) => call(settings, `/tasks/${encodeURIComponent(task.id)}/draft`, 'PUT', { revision: task.revision, payload })
export const confirmDraft = (settings, task) => call(settings, `/tasks/${encodeURIComponent(task.id)}/confirm`, 'POST', {
  revision: task.revision, payload_hash: task.payload_hash, idempotency_key: task.idempotency_key
})
export const cancelTask = (settings, task) => call(settings, `/tasks/${encodeURIComponent(task.id)}/cancel`, 'POST', { revision: task.revision })
export const taskEvents = (settings, task) => call(settings, `/tasks/${encodeURIComponent(task.id)}/events`)
export const updateTicket = (settings, ticket, status) => call(settings, `/tickets/${encodeURIComponent(ticket.id)}`, 'PATCH', { status, version: ticket.version })
export const knowledgeDocuments = (settings) => call(settings, '/knowledge/documents')
export const replaceDocument = (settings, id, payload) => call(settings, `/knowledge/documents/${encodeURIComponent(id)}`, 'PUT', payload)
export const deleteDocument = (settings, id) => call(settings, `/knowledge/documents/${encodeURIComponent(id)}`, 'DELETE')
export const evaluationReports = (settings) => call(settings, '/eval/reports')
export const evaluationReport = (settings, id) => call(settings, `/eval/reports/${encodeURIComponent(id)}`)
export const promoteEvaluation = (settings, id) => call(settings, `/eval/reports/${encodeURIComponent(id)}/baseline`, 'POST', {})
