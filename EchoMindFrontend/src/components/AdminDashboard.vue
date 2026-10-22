<template>
  <section class="page page-admin">
    <div class="page-heading">
      <div class="heading-copy"><span class="kicker">Organization administration</span><h1>组织后台</h1><p>查看本组织的用户、操作记录、会话与工单。</p></div>
      <button v-if="admin" @click="refresh" :disabled="loading[activeTab]">{{ loading[activeTab] ? '加载中…' : '刷新当前页面' }}</button>
    </div>
    <section v-if="!admin || accessError" class="workspace-card" role="alert">
      <h2>{{ accessError?.status === 401 ? '登录已失效' : '无后台权限' }}</h2>
      <p>{{ accessError?.message || '当前账号无管理员后台权限，请联系组织管理员。' }}</p>
    </section>
    <template v-else>
      <nav class="admin-tabs" aria-label="后台内容">
        <button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }" :aria-pressed="activeTab === tab.id" @click="activeTab = tab.id">{{ tab.label }}</button>
      </nav>
      <p v-if="errors[activeTab]" class="danger" role="alert">{{ errors[activeTab] }}</p>
      <p v-if="loading[activeTab]" class="muted" role="status">正在加载本组织数据…</p>

      <div v-if="activeTab === 'overview' && overview" class="admin-overview">
        <section class="admin-counts">
          <article v-for="metric in countLabels" :key="metric.id" class="workspace-card"><span>{{ metric.label }}</span><strong>{{ overview.counts[metric.id] }}</strong></article>
        </section>
        <section class="workspace-card"><h2>任务状态</h2><div class="status-counts"><span v-for="(count, status) in overview.task_status" :key="status">{{ statusLabel(status) }} <strong>{{ count }}</strong></span></div><p v-if="!Object.keys(overview.task_status).length" class="muted">还没有任务。保存工单草稿后，可以在这里跟进确认进度。</p></section>
        <section class="workspace-card"><h2>工单状态</h2><div class="status-counts"><span v-for="(count, status) in overview.ticket_status" :key="status">{{ statusLabel(status) }} <strong>{{ count }}</strong></span></div><p v-if="!Object.keys(overview.ticket_status).length" class="muted">还没有已提交的工单。确认草稿后，处理状态会显示在这里。</p></section>
      </div>

      <section v-if="activeTab === 'users'" class="workspace-card">
        <h2>本组织用户</h2>
        <div class="admin-table-wrap"><table><thead><tr><th>姓名 / 用户编号</th><th>邮箱</th><th>角色</th><th>创建时间</th></tr></thead><tbody><tr v-for="user in pages.users.items" :key="user.id"><td>{{ user.display_name || '未命名用户' }}<small>{{ user.id }}</small></td><td>{{ user.email || '-' }}</td><td>{{ roleLabel(user.role) }}</td><td>{{ date(user.created_at) }}</td></tr></tbody></table></div>
        <p v-if="!loading.users && !pages.users.items.length" class="workspace-empty">本页没有用户。</p>
      </section>
      <section v-if="activeTab === 'activity'" class="workspace-card">
        <h2>组织审计记录</h2>
        <article v-for="event in pages.activity.items" :key="event.id" class="admin-record"><div><strong>{{ event.event_type }}</strong><small>{{ date(event.created_at) }} · {{ event.actor_display_name || event.actor_id || '系统' }}</small></div><p v-if="event.task_id">任务：{{ event.task_id }}</p><p v-if="event.ticket_id">工单：{{ event.ticket_id }}</p><dl v-if="Object.keys(event.details).length" class="admin-details"><template v-for="(value, key) in event.details" :key="key"><dt>{{ detailLabel(key) }}</dt><dd>{{ value }}</dd></template></dl></article>
        <p v-if="!loading.activity && !pages.activity.items.length" class="workspace-empty">本页没有审计记录。</p>
      </section>
      <section v-if="activeTab === 'conversations'" class="workspace-card">
        <h2>组织会话索引</h2>
        <div class="admin-table-wrap"><table><thead><tr><th>会话编号</th><th>所属用户</th><th>请求数</th><th>创建时间</th></tr></thead><tbody><tr v-for="conversation in pages.conversations.items" :key="conversation.conversation_id"><td>{{ conversation.conversation_id }}</td><td>{{ conversation.owner_display_name || conversation.owner_id || '-' }}</td><td>{{ conversation.request_count ?? '-' }}</td><td>{{ date(conversation.created_at) }}</td></tr></tbody></table></div>
        <p v-if="!loading.conversations && !pages.conversations.items.length" class="workspace-empty">本页没有会话。</p>
      </section>
      <section v-if="activeTab === 'tickets'" class="workspace-card">
        <div class="card-heading"><h2>本组织全部工单</h2><span>共 {{ pages.tickets.total }} 份</span></div>
        <article v-for="ticket in pages.tickets.items" :key="ticket.id" class="admin-record"><div class="record-heading"><strong>{{ ticket.title || '未命名工单' }}</strong><span>{{ statusLabel(ticket.status) }}</span></div><small>{{ ticket.id }} · {{ date(ticket.created_at) }}</small><p class="record-description">{{ ticket.description }}</p><small>{{ categoryLabel(ticket.category) }} · {{ priorityLabel(ticket.priority) }}<template v-if="ticket.created_by"> · 用户 {{ ticket.created_by }}</template></small></article>
        <p v-if="!loading.tickets && !pages.tickets.items.length" class="workspace-empty">本页没有工单。</p>
      </section>
      <section v-if="activeTab === 'tasks'" class="workspace-card">
        <div class="card-heading"><h2>本组织全部任务</h2><span>共 {{ pages.tasks.total }} 份</span></div>
        <article v-for="task in pages.tasks.items" :key="task.id" class="admin-record"><div class="record-heading"><strong>{{ task.payload.title || '未命名任务' }}</strong><span>{{ statusLabel(task.status) }}</span></div><small>{{ task.id }} · {{ date(task.created_at) }}<template v-if="task.created_by"> · 用户 {{ task.created_by }}</template></small><p class="record-description">{{ task.payload.description }}</p><small>{{ categoryLabel(task.payload.category) }} · {{ priorityLabel(task.payload.priority) }}<template v-if="task.status === 'waiting_confirmation'"> · 确认有效至 {{ date(task.expires_at) }}</template><template v-if="task.ticket_id"> · 工单 {{ task.ticket_id }}</template></small></article>
        <p v-if="!loading.tasks && !pages.tasks.items.length" class="workspace-empty">本页没有任务。</p>
      </section>
      <div v-if="currentPage" class="admin-pagination">
        <span>共 {{ currentPage.total }} 条 · {{ currentPage.total ? currentPage.offset + 1 : 0 }}–{{ Math.min(currentPage.offset + currentPage.items.length, currentPage.total) }}</span>
        <label>每页 <select :value="currentPage.limit" @change="changePageSize" :disabled="loading[activeTab]"><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option></select></label>
        <button class="quiet-button" @click="movePage(-1)" :disabled="loading[activeTab] || currentPage.offset === 0">上一页</button>
        <button class="quiet-button" @click="movePage(1)" :disabled="loading[activeTab] || currentPage.offset + currentPage.limit >= currentPage.total">下一页</button>
      </div>
    </template>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { fetchAdminOverview, fetchAdminUsers, fetchAdminActivity, fetchAdminConversations, fetchAdminTasks, fetchAdminTickets, isAdmin } from '../lib/admin.js'

const props = defineProps({ settings: { type: Object, required: true }, session: { type: Object, default: null } })
const emit = defineEmits(['notice', 'session-expired'])
const tabs = [{ id: 'overview', label: '总览' }, { id: 'users', label: '用户' }, { id: 'activity', label: '审计记录' }, { id: 'conversations', label: '会话索引' }, { id: 'tickets', label: '全部工单' }, { id: 'tasks', label: '全部任务' }]
const countLabels = [{ id: 'users', label: '用户' }, { id: 'tasks', label: '任务' }, { id: 'tickets', label: '工单' }, { id: 'events', label: '操作记录' }, { id: 'conversations', label: '会话' }, { id: 'requests', label: '请求' }]
const activeTab = ref('overview'), overview = ref(null), accessError = ref(null)
const emptyPage = () => ({ items: [], total: 0, limit: 20, offset: 0 })
const pages = reactive({ users: emptyPage(), activity: emptyPage(), conversations: emptyPage(), tickets: emptyPage(), tasks: emptyPage() })
const loading = reactive({}), errors = reactive({}), loaded = reactive({})
const admin = computed(() => isAdmin(props.session))
const currentPage = computed(() => pages[activeTab.value] || null)
const requests = new Map(), sequences = new Map()
let identityGeneration = 0
const loaders = { overview: fetchAdminOverview, users: fetchAdminUsers, activity: fetchAdminActivity, conversations: fetchAdminConversations, tickets: fetchAdminTickets, tasks: fetchAdminTasks }

function clearData() {
  overview.value = null
  for (const key of Object.keys(pages)) pages[key] = emptyPage()
  for (const tab of tabs) { loading[tab.id] = false; errors[tab.id] = ''; loaded[tab.id] = false }
}
function invalidate() {
  identityGeneration += 1
  for (const controller of requests.values()) controller.abort()
  requests.clear(); sequences.clear()
}
async function load(tab, pagination = {}) {
  if (!admin.value || !loaders[tab]) return
  const generation = identityGeneration
  const sequence = (sequences.get(tab) || 0) + 1
  sequences.set(tab, sequence)
  requests.get(tab)?.abort()
  const controller = new AbortController()
  requests.set(tab, controller)
  loading[tab] = true; errors[tab] = ''
  const options = { ...(pages[tab] ? { limit: pages[tab].limit, offset: pages[tab].offset } : {}), ...pagination, signal: controller.signal }
  const current = () => generation === identityGeneration && sequences.get(tab) === sequence && admin.value
  try {
    const data = await loaders[tab](props.settings, props.session, options)
    if (!current()) return
    if (pages[tab]) pages[tab] = data
    else if (tab === 'overview') overview.value = data
    loaded[tab] = true
  } catch (failure) {
    if (!current() || failure?.name === 'AbortError') return
    if (failure.status === 401 || failure.status === 403) {
      invalidate(); clearData(); accessError.value = { status: failure.status, message: failure.message }
      emit('notice', failure.message)
      emit('session-expired', { status: failure.status })
    } else errors[tab] = failure.message || '后台加载失败'
  } finally {
    if (current()) { loading[tab] = false; requests.delete(tab) }
  }
}
function refresh() { accessError.value = null; return load(activeTab.value) }
function movePage(direction) { if (currentPage.value) load(activeTab.value, { offset: Math.max(0, currentPage.value.offset + direction * currentPage.value.limit) }) }
function changePageSize(event) { load(activeTab.value, { limit: Number(event.target.value), offset: 0 }) }
function date(value) {
  if (value === undefined || value === null || value === '') return '-'
  const parsed = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(parsed.getTime()) ? '-' : parsed.toLocaleString()
}
function statusLabel(value) { return ({ waiting_confirmation: '待确认', completed: '已提交', cancelled: '已取消', rejected: '已拒绝', expired: '已过期', open: '待受理', in_progress: '处理中', resolved: '已解决', closed: '已关闭' })[value] || value || '-' }
function roleLabel(value) { return ({ admin: '管理员', support: '客服', kb_admin: '知识库管理员', customer: '客户' })[value] || value || '-' }
function categoryLabel(value) { return ({ technical: '技术支持', billing: '账单', general: '一般问题' })[value] || value || '-' }
function priorityLabel(value) { return ({ low: '低', normal: '普通', high: '高', urgent: '紧急' })[value] || value || '-' }
function detailLabel(value) { return ({ status: '状态', from_status: '原状态', to_status: '新状态', previous_status: '原状态', revision: '修订', version: '版本', category: '分类', priority: '优先级', document_id: '文档', document_version: '文档版本', knowledge_base_id: '知识库' })[value] || value }

watch(() => [props.session, props.session?.user_id, props.session?.user?.id, props.session?.organization_id, props.session?.role, props.settings?.endpoints?.python], () => {
  invalidate(); clearData(); accessError.value = null; activeTab.value = 'overview'
  if (admin.value) load('overview')
}, { immediate: true, flush: 'sync' })
watch(activeTab, tab => { if (admin.value && !accessError.value && !loaded[tab] && !loading[tab]) load(tab) })
onBeforeUnmount(invalidate)
</script>
