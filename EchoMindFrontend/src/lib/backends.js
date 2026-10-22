import { readChatStream } from './chat-stream.js'

const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: runtimeConfig().pythonApiUrl || import.meta.env?.VITE_PYTHON_API_URL || '/api/python',
    port: '8000'
  },
  java: {
    id: 'java',
    label: 'Java',
    baseUrl: runtimeConfig().javaApiUrl || import.meta.env?.VITE_JAVA_API_URL || '/api/java',
    port: '8080'
  }
}

export function availableBackendIds() {
  const configured = runtimeConfig().enabledBackends
  if (!Array.isArray(configured)) return ['python', 'java']
  const enabled = configured.filter((id) => Object.hasOwn(DEFAULT_BACKENDS, id))
  return enabled.length ? enabled : ['python']
}

export function createInitialSettings() {
  const saved = readSettings()
  const available = availableBackendIds()
  const preferred = saved.backend || import.meta.env?.VITE_DEFAULT_BACKEND || 'python'
  return {
    backend: available.includes(preferred) ? preferred : available[0],
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    endpoints: {
      python: saved.endpoints?.python || DEFAULT_BACKENDS.python.baseUrl,
      java: saved.endpoints?.java || DEFAULT_BACKENDS.java.baseUrl
    }
  }
}

export function saveSettings(settings) {
  localStorage.setItem('echomind.frontend.settings', JSON.stringify(settings))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.java
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/health')
}

export async function requestMonitor(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/monitor')
}

export async function requestSkills(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/skills')
}

export async function reloadSkills(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/skills/reload', { method: 'POST' })
}

export async function requestKnowledgeStats(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/stats')
}

export async function runEvaluation(type, settings, body = null) {
  return requestJson(backendMeta(type, settings).baseUrl, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  })
}

export async function requestSearch(type, settings, query, topK = 5, strategy = 'vector', rewrite = true, filters = {}) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  if (type === 'python') { params.set('strategy', strategy); params.set('rewrite', String(rewrite)) }
  if (type === 'python') {
    for (const key of ['product_version', 'knowledge_base_id']) if (filters[key]?.trim()) params.set(key, filters[key].trim())
  }
  return requestJson(backendMeta(type, settings).baseUrl, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message, signal) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const raw = await requestJson(meta.baseUrl, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal
  })
  return normalizeChatResponse(type, raw)
}

export async function requestChatStream(type, settings, message, onEvent, signal) {
  if (type !== 'python') return requestChat(type, settings, message, signal)
  const response = await fetch(`${backendMeta(type, settings).baseUrl}/chat/stream`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(buildChatPayload(type, settings, message)),
    signal
  })
  if (response.status === 404 || response.status === 405) {
    onEvent?.({ type: 'stage', message: '当前后端尚未启用实时回复，等待完整结果' })
    return requestChat(type, settings, message, signal)
  }
  if (!response.ok) {
    const error = new Error(`请求失败（${response.status}）：${await response.text()}`)
    error.status = response.status
    throw error
  }
  return normalizeChatResponse(type, await readChatStream(response, onEvent))
}

export async function requestToolTrace(type, settings, requestId) {
  if (!requestId) return null
  const raw = await requestJson(backendMeta(type, settings).baseUrl, `/trace/tool/${encodeURIComponent(requestId)}`)
  return normalizeToolTraceResponse(raw)
}

export async function addKnowledge(type, settings, documents) {
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

function buildChatPayload(type, settings, message) {
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    agentTypes: raw.agent_types || raw.agentTypes || [],
    primaryAgent: raw.primary_agent || raw.primaryAgent || '',
    supportingAgents: raw.supporting_agents || raw.supportingAgents || [],
    routingReason: raw.routing_reason || raw.routingReason || '',
    routingConfidence: Number(raw.routing_confidence ?? raw.routingConfidence ?? 0),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? raw.intentConfidence ?? 0),
    intentSourceScores: raw.intent_source_scores || raw.intentSourceScores || {},
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    verified: raw.verified,
    grounded: raw.grounded,
    sources: raw.sources || [],
    tasks: raw.tasks || [],
    warnings: raw.warnings || [],
    feedbackAvailable: Boolean(raw.feedback_available),
    durationMs: Number(raw.duration_ms || 0),
    raw
  }
}

function normalizeToolTraceResponse(raw) {
  const trace = raw?.trace || {}
  return {
    requestId: raw?.request_id || raw?.requestId || '',
    found: Boolean(raw?.found),
    trace: {
      ...trace,
      toolsUsed: trace.tools_used || trace.toolsUsed || [],
      toolCalls: trace.tool_calls || trace.toolCalls || []
    },
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, { credentials: 'include', ...options })
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : data?.detail?.message || data?.detail || JSON.stringify(data)
    const error = new Error(`${response.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
    error.status = response.status
    throw error
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('echomind.frontend.settings') || '{}')
  } catch {
    return {}
  }
}

function runtimeConfig() {
  if (typeof window === 'undefined') return {}
  return window.__ECHOMIND_CONFIG__ || {}
}
