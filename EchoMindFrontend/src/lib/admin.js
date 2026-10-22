import { backendMeta } from './backends.js'

export class AdminApiError extends Error {
  constructor(message, status, code = 'admin_request_failed') {
    super(message)
    this.name = 'AdminApiError'
    this.status = status
    this.code = code
  }
}

export function isAdmin(session) { return session?.role === 'admin' }

const failShape = () => { throw new AdminApiError('后台返回的数据格式异常，请刷新后重试', 502, 'invalid_response') }
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const integer = value => Number.isSafeInteger(value) && value >= 0
function pick(value, fields) {
  if (!object(value)) return failShape()
  return Object.fromEntries(fields.filter(key => Object.hasOwn(value, key)).map(key => [key, value[key]]))
}
function scalarFields(value, fields) {
  const result = pick(value, fields)
  for (const item of Object.values(result)) {
    if (item !== null && !['string', 'number', 'boolean'].includes(typeof item)) return failShape()
    if (typeof item === 'number' && !Number.isFinite(item)) return failShape()
  }
  return result
}

async function call(settings, session, path, options = {}) {
  if (!isAdmin(session)) throw new AdminApiError('当前账号无管理员后台权限', 403, 'admin_required')
  let response
  try {
    response = await fetch(backendMeta('python', settings).baseUrl + path, {
      method: 'GET', credentials: 'include', signal: options.signal
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new AdminApiError('无法连接后台，请检查连接后重试', 0, 'network_error')
  }
  let data
  try { data = await response.json() } catch {
    throw new AdminApiError(`后台返回异常（${response.status}）`, response.ok ? 502 : response.status, 'invalid_response')
  }
  if (!response.ok) {
    const detail = object(data?.detail) ? data.detail : null
    const fallback = response.status === 401 ? '登录已失效，请重新登录' : response.status === 403 ? '当前账号无管理员后台权限' : '后台请求失败'
    const message = typeof detail?.message === 'string' ? detail.message : typeof data?.detail === 'string' ? data.detail : fallback
    throw new AdminApiError(message, response.status, typeof detail?.code === 'string' ? detail.code : 'admin_request_failed')
  }
  if (!object(data)) return failShape()
  return data
}

function pageOptions(options = {}) {
  const limit = options.limit ?? 20
  const offset = options.offset ?? 0
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 || !integer(offset)) {
    throw new AdminApiError('分页参数无效', 400, 'invalid_pagination')
  }
  return { limit, offset }
}

async function page(settings, session, path, normalize, options = {}) {
  if (!isAdmin(session)) throw new AdminApiError('当前账号无管理员后台权限', 403, 'admin_required')
  const requested = pageOptions(options)
  const query = new URLSearchParams(requested)
  const data = await call(settings, session, `${path}?${query}`, options)
  if (!Array.isArray(data.items) || !integer(data.total) || !integer(data.offset) ||
      data.limit !== requested.limit || data.offset !== requested.offset ||
      data.items.length > data.limit || data.items.length > data.total ||
      (data.items.length > 0 && data.offset + data.items.length > data.total)) return failShape()
  return { items: data.items.map(normalize), total: data.total, limit: data.limit, offset: data.offset }
}

const user = value => scalarFields(value, ['id', 'email', 'display_name', 'role', 'created_at'])
const conversation = value => scalarFields(value, ['conversation_id', 'owner_id', 'owner_display_name', 'created_at', 'request_count'])
const detailFields = ['status', 'from_status', 'to_status', 'previous_status', 'revision', 'version', 'category', 'priority', 'document_id', 'document_version', 'knowledge_base_id']
const audit = value => ({
  ...scalarFields(value, ['id', 'event_type', 'actor_id', 'actor_display_name', 'task_id', 'ticket_id', 'created_at']),
  details: object(value.details) ? scalarFields(value.details, detailFields) : {}
})
const ticket = value => scalarFields(value, ['id', 'title', 'description', 'category', 'priority', 'status', 'created_by', 'version', 'created_at', 'updated_at'])
const task = value => ({
  ...scalarFields(value, ['id', 'status', 'created_by', 'revision', 'ticket_id', 'created_at', 'updated_at', 'expires_at']),
  payload: object(value.payload) ? scalarFields(value.payload, ['title', 'description', 'category', 'priority']) : {}
})

export async function fetchAdminOverview(settings, session, options = {}) {
  const data = await call(settings, session, '/admin/overview', options)
  if (!object(data.counts) || !object(data.task_status) || !object(data.ticket_status)) return failShape()
  const countFields = ['users', 'tasks', 'tickets', 'events', 'conversations', 'requests']
  const counts = pick(data.counts, countFields)
  if (countFields.some(key => !integer(counts[key]))) return failShape()
  const taskStatus = pick(data.task_status, ['waiting_confirmation', 'completed', 'cancelled', 'rejected', 'expired'])
  const ticketStatus = pick(data.ticket_status, ['open', 'in_progress', 'resolved', 'closed', 'cancelled'])
  if ([...Object.values(taskStatus), ...Object.values(ticketStatus)].some(value => !integer(value))) return failShape()
  return { counts, task_status: taskStatus, ticket_status: ticketStatus }
}

export const fetchAdminUsers = (settings, session, options) => page(settings, session, '/admin/users', user, options)
export const fetchAdminActivity = (settings, session, options) => page(settings, session, '/admin/activity', audit, options)
export const fetchAdminConversations = (settings, session, options) => page(settings, session, '/admin/conversations', conversation, options)

export const fetchAdminTasks = (settings, session, options) => page(settings, session, '/admin/tasks', task, options)
export const fetchAdminTickets = (settings, session, options) => page(settings, session, '/admin/tickets', ticket, options)
