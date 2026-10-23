<template>
  <section class="account-strip" aria-label="登录状态">
    <template v-if="session">
      <div class="account-identity"><span class="account-avatar" aria-hidden="true">{{ (session.user?.display_name || session.user_id || 'U').slice(0, 1).toUpperCase() }}</span><strong>{{ session.user?.display_name || session.user_id?.slice(0, 10) }}</strong><span class="account-role">{{ ({ admin: '管理员', support: '客服', kb_admin: '知识库管理员', customer: '用户' })[session.role] || '用户' }}</span><small v-if="session.organization?.name" class="account-organization">{{ session.organization.name }}</small></div>
      <button class="quiet-button" @click="signOut" :disabled="pending">退出登录</button>
    </template>
    <form v-else class="login-line" @submit.prevent="registerMode ? signUp() : signIn()">
      <span class="login-intro">{{ registerMode ? '创建你的账号' : '登录你的工作空间' }}</span>
      <label><span class="sr-label">用户名</span><input v-model="username" type="text" autocomplete="username" :pattern="registerMode ? '[A-Za-z0-9_]{3,32}' : undefined" :maxlength="registerMode ? 32 : 254" :title="registerMode ? '3–32 位字母、数字或下划线' : '请输入用户名'" placeholder="用户名" required /></label>
      <label><span class="sr-label">密码</span><input v-model="password" type="password" :autocomplete="registerMode ? 'new-password' : 'current-password'" :minlength="registerMode ? 12 : 1" maxlength="1024" :placeholder="registerMode ? '密码（至少12位）' : '登录密码'" required /></label>
      <label v-if="registerMode"><span class="sr-label">确认密码</span><input v-model="confirmPassword" type="password" autocomplete="new-password" placeholder="再次输入密码" required maxlength="1024" /></label>
      <button :disabled="pending">{{ pending ? '请稍候…' : registerMode ? '注册账号' : '登录' }}</button>
      <button type="button" class="quiet-button" :disabled="pending" @click="toggleAuth">{{ registerMode ? '已有账号？去登录' : '没有账号？立即注册' }}</button>
      <small v-if="registerMode">用户名为3–32位字母、数字或下划线，注册后可管理自己的咨询与工单。</small>
    </form>
    <span v-if="error" class="danger" role="alert">{{ error }}</span>
  </section>
  <section v-if="visible" class="page page-tickets">
    <div class="page-heading">
      <div class="heading-copy"><span class="kicker">Support desk</span><h1>{{ canManage ? '组织工单与待办' : '我的工单与待办' }}</h1><p>{{ canManage ? '查看本组织工单、处理状态与操作记录。' : '仅展示当前账号自己的草稿与工单，核对后可确认提交。' }}</p></div>
      <button @click="handle(refresh)" :disabled="pending || !session">刷新</button>
    </div>
    <p v-if="!session" class="workspace-empty">登录后可创建、确认和查看自己的工单。</p>
    <div v-else class="operations-grid">
      <section class="workspace-card">
        <div class="card-heading"><h2>{{ selected ? '核对工单草稿' : '新建工单草稿' }}</h2><span v-if="selected">第 {{ selected.revision }} 版</span></div>
        <form @submit.prevent="save">
          <label><span>标题</span><input v-model="form.title" maxlength="160" required /></label>
          <label><span>问题描述</span><textarea v-model="form.description" rows="7" maxlength="10000" required /></label>
          <div class="form-pair">
            <label><span>分类</span><select v-model="form.category"><option value="technical">技术支持</option><option value="billing">账单</option><option value="general">一般问题</option></select></label>
            <label><span>优先级</span><select v-model="form.priority"><option value="low">低</option><option value="normal">普通</option><option value="high">高</option><option value="urgent">紧急</option></select></label>
          </div>
          <p v-if="selected">状态：{{ statusLabel(selected.status) }} · 确认有效至 {{ date(selected.confirmation_expires_at) }}</p>
          <div class="side-actions">
            <button type="submit" :disabled="pending || (selected && selected.status !== 'waiting_confirmation')">{{ selected ? '保存修改' : '保存为待确认草稿' }}</button>
            <button type="button" class="quiet-button" @click="newDraft" :disabled="pending">新建</button>
          </div>
        </form>
        <div v-if="selected?.status === 'waiting_confirmation'" class="confirmation-box">
          <p>{{ dirty ? '内容已修改，请先保存新版本，再确认提交。' : '确认后会创建工单，并返回可查询的工单编号。' }}</p>
          <div class="side-actions">
            <button @click="confirm" :disabled="pending || dirty">确认提交工单</button>
            <button class="quiet-button" @click="cancel" :disabled="pending">取消草稿</button>
          </div>
        </div>
        <p v-if="lastTicket" class="success" role="status">工单已创建：{{ lastTicket.id }}</p>
        <details v-if="canReadAudit && events.length"><summary>操作历史</summary><p v-for="event in events" :key="event.id">{{ date(event.created_at) }} · {{ event.event_type }}</p></details>
      </section>
      <section class="workspace-card">
        <div class="card-heading"><h2>待办与草稿</h2><span>{{ taskList.length }}</span></div>
        <button v-for="task in taskList" :key="task.id" class="task-row" @click="select(task)" :disabled="pending">
          <strong>{{ task.payload.title }}</strong><span>{{ statusLabel(task.status) }}</span><small>{{ date(task.updated_at) }}</small>
        </button>
        <p v-if="!taskList.length" class="workspace-empty">暂无任务。也可以在对话中请求创建工单。</p>
      </section>
      <section class="workspace-card ticket-list-card">
        <div class="card-heading"><h2>已提交工单</h2><span>{{ ticketList.length }}</span></div>
        <article v-for="ticket in ticketList" :key="ticket.id" class="ticket-row">
          <div><strong>{{ ticket.title }}</strong><small>{{ ticket.id }}</small><p>{{ ticket.description }}</p></div>
          <div><span>{{ statusLabel(ticket.status) }}</span>
            <select v-if="canManage" :value="ticket.status" @change="changeStatus(ticket, $event.target.value)" :disabled="pending">
              <option :value="ticket.status">{{ statusLabel(ticket.status) }}</option>
              <option v-for="next in transitions[ticket.status] || []" :key="next" :value="next">{{ statusLabel(next) }}</option>
            </select>
          </div>
        </article>
        <p v-if="!ticketList.length" class="workspace-empty">确认草稿后，工单会出现在这里。</p>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../lib/operations.js'
const props = defineProps({ settings: { type: Object, required: true }, visible: Boolean, refreshKey: Number })
const emit = defineEmits(['session-change', 'notice'])
const session = ref(null), username = ref(''), password = ref(''), error = ref(''), pending = ref(false)
const registerMode = ref(false), confirmPassword = ref('')
const taskList = ref([]), ticketList = ref([]), selected = ref(null), events = ref([]), lastTicket = ref(null)
const blank = () => ({ title: '', description: '', category: 'technical', priority: 'normal', metadata: {} })
const form = reactive(blank())
function newKey() { return globalThis.crypto?.randomUUID?.() || `draft-${Date.now()}-${Math.random().toString(36).slice(2)}` }
let createKey = newKey()
const transitions = { open: ['in_progress', 'cancelled'], in_progress: ['resolved', 'cancelled'], resolved: ['closed', 'in_progress'] }
const canManage = computed(() => ['support', 'admin'].includes(session.value?.role))
const canReadAudit = computed(() => canManage.value)
let generation = 0, refreshSequence = 0, selectionSequence = 0, pendingWork = 0
const identity = value => value ? `${value.user_id || value.user?.id || ''}:${value.organization_id || ''}:${value.role || ''}` : ''
function context() {
  const currentGeneration = generation
  return { current: () => currentGeneration === generation }
}
function canonical(value) { return JSON.stringify(value, Object.keys(value).sort()) }
const dirty = computed(() => selected.value && (
  ['title', 'description', 'category', 'priority'].some(key => form[key] !== selected.value.payload[key]) ||
  canonical(form.metadata) !== canonical(selected.value.payload.metadata)))
function statusLabel(value) { return ({ waiting_confirmation: '待确认', completed: '已提交', cancelled: '已取消', rejected: '已拒绝', expired: '已过期', open: '待受理', in_progress: '处理中', resolved: '已解决', closed: '已关闭' })[value] || value }
function date(value) { return value ? new Date(value * 1000).toLocaleString() : '-' }
function clear() { selectionSequence += 1; selected.value = null; events.value = []; lastTicket.value = null; Object.assign(form, blank()); createKey = newKey() }
function newDraft() { clear() }
function clearPrivateState() {
  generation += 1; refreshSequence += 1; pendingWork = 0; pending.value = false
  taskList.value = []; ticketList.value = []; clear()
}
function setSession(value) {
  if (identity(value) !== identity(session.value)) clearPrivateState()
  session.value = value
  emit('session-change', value)
}
function invalidateSession() {
  clearPrivateState(); session.value = null; password.value = ''; username.value = ''; confirmPassword.value = ''; error.value = ''
  emit('session-change', null)
}
defineExpose({ invalidateSession })

async function recoverForbidden(message) {
  // Re-read the server profile once after revocation; never retry the denied operation.
  invalidateSession()
  const recovery = context()
  try {
    const profile = await api.sessionInfo(props.settings)
    if (!recovery.current()) return
    setSession(profile); error.value = message
  } catch {
    if (recovery.current()) error.value = message
  }
}
async function handle(work) {
  const operation = context()
  error.value = ''; pendingWork += 1; pending.value = true
  try { return await work(operation) }
  catch (failure) {
    if (!operation.current()) return
    if (failure.status === 401) { invalidateSession(); error.value = failure.message }
    else if (failure.status === 403) await recoverForbidden(failure.message)
    else error.value = failure.message || '操作失败，请稍后重试'
  } finally {
    if (operation.current()) { pendingWork -= 1; pending.value = pendingWork > 0 }
  }
}
async function refresh(operation = context()) {
  if (!session.value || !operation.current()) return
  const sequence = ++refreshSequence
  const [taskData, ticketData] = await Promise.all([api.tasks(props.settings), api.tickets(props.settings)])
  if (!operation.current() || sequence !== refreshSequence) return
  taskList.value = taskData.items; ticketList.value = ticketData.items
}
function toggleAuth() {
  if (pending.value) return
  generation += 1 // Ignore a late mount-time profile probe while filling the form.
  registerMode.value = !registerMode.value
  password.value = ''; confirmPassword.value = ''; error.value = ''
}
async function signUp() {
  if (pending.value) return
  if (password.value !== confirmPassword.value) { error.value = '两次输入的密码不一致'; return }
  generation += 1
  await handle(async operation => {
    const result = await api.register(props.settings, username.value.trim(), password.value)
    if (!operation.current()) return
    username.value = result.username; password.value = ''; confirmPassword.value = ''
    registerMode.value = false
    emit('notice', '注册成功，请使用用户名和密码登录')
  })
}
async function signIn() {
  if (pending.value) return
  const loginEmail = username.value.trim(), loginPassword = password.value
  // Invalidate any mount-time session probe before starting a new login.
  invalidateSession()
  username.value = loginEmail
  await handle(async operation => {
    await api.login(props.settings, loginEmail, loginPassword)
    if (!operation.current()) return
    const profile = await api.sessionInfo(props.settings)
    if (!operation.current()) return
    setSession(profile)
    if (props.visible) await handle(refresh)
  })
  if (!session.value) username.value = loginEmail
}
async function signOut() {
  // Clear personal data immediately, including responses still in flight.
  invalidateSession()
  await handle(async () => { await api.logout(props.settings) })
}
async function select(task) {
  selectionSequence += 1; events.value = []
  selected.value = task; lastTicket.value = null
  Object.assign(form, JSON.parse(JSON.stringify(task.payload)))
  if (!canReadAudit.value) return
  const sequence = selectionSequence
  await handle(async operation => {
    const result = await api.taskEvents(props.settings, task)
    if (operation.current() && sequence === selectionSequence && canReadAudit.value) events.value = result.items
  })
}
async function save() {
  await handle(async operation => {
    const payload = JSON.parse(JSON.stringify(form))
    const task = selected.value ? await api.reviseDraft(props.settings, selected.value, payload) : await api.createDraft(props.settings, payload, createKey)
    if (!operation.current()) return
    selectionSequence += 1; events.value = []
    selected.value = task; Object.assign(form, JSON.parse(JSON.stringify(task.payload)))
    await refresh(operation)
    if (operation.current()) emit('notice', '草稿已保存，请核对后确认')
  })
}
async function confirm() {
  if (!selected.value) return
  await handle(async operation => {
    const result = await api.confirmDraft(props.settings, selected.value)
    if (!operation.current()) return
    const sequence = ++selectionSequence
    selected.value = result.task; lastTicket.value = result.ticket; events.value = []
    await refresh(operation)
    if (!operation.current() || !canReadAudit.value || sequence !== selectionSequence) return
    const history = await api.taskEvents(props.settings, result.task)
    if (operation.current() && sequence === selectionSequence && canReadAudit.value) events.value = history.items
  })
}
async function cancel() {
  if (!selected.value) return
  await handle(async operation => {
    const task = await api.cancelTask(props.settings, selected.value)
    if (!operation.current()) return
    selectionSequence += 1; selected.value = task; events.value = []
    await refresh(operation)
  })
}
async function changeStatus(ticket, value) {
  await handle(async operation => {
    await api.updateTicket(props.settings, ticket, value)
    if (operation.current()) await refresh(operation)
  })
}
onMounted(async () => {
  const restore = context()
  try {
    const profile = await api.sessionInfo(props.settings)
    if (!restore.current()) return
    setSession(profile)
    if (props.visible) await handle(refresh)
  } catch {
    if (restore.current()) invalidateSession()
  }
})
watch(() => [props.visible, props.refreshKey], () => {
  if (props.visible && session.value && !pending.value) handle(refresh)
})
onBeforeUnmount(() => { generation += 1 })
</script>
