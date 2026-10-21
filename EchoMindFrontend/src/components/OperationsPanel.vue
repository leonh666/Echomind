<template>
  <section class="account-strip" aria-label="登录状态">
    <template v-if="session">
      <span>已登录 · {{ session.user?.display_name || session.user_id?.slice(0, 10) }} · {{ session.role }}</span>
      <button class="quiet-button" @click="signOut" :disabled="pending">退出登录</button>
    </template>
    <form v-else class="login-line" @submit.prevent="signIn">
      <label><span class="sr-label">邮箱</span><input v-model="email" type="email" autocomplete="username" placeholder="账号邮箱" required /></label>
      <label><span class="sr-label">密码</span><input v-model="password" type="password" autocomplete="current-password" placeholder="登录密码" required /></label>
      <button :disabled="pending">登录</button>
      <small>尚无账号？请联系管理员开通。</small>
    </form>
    <span v-if="error" class="danger" role="alert">{{ error }}</span>
  </section>
  <section v-if="visible" class="page page-tickets">
    <div class="page-heading">
      <div class="heading-copy"><span class="kicker">Support desk</span><h1>工单与待办</h1><p>核对草稿后提交，随时查看处理状态与历史记录。</p></div>
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
            <button type="button" class="quiet-button" @click="newDraft">新建</button>
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
        <details v-if="events.length"><summary>操作历史</summary><p v-for="event in events" :key="event.id">{{ date(event.created_at) }} · {{ event.event_type }}</p></details>
      </section>
      <section class="workspace-card">
        <div class="card-heading"><h2>待办与草稿</h2><span>{{ taskList.length }}</span></div>
        <button v-for="task in taskList" :key="task.id" class="task-row" @click="select(task)">
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../lib/operations.js'
const props = defineProps({ settings: { type: Object, required: true }, visible: Boolean, refreshKey: Number })
const emit = defineEmits(['session-change', 'notice'])
const session = ref(null), email = ref(''), password = ref(''), error = ref(''), pending = ref(false)
const taskList = ref([]), ticketList = ref([]), selected = ref(null), events = ref([]), lastTicket = ref(null)
const blank = () => ({ title: '', description: '', category: 'technical', priority: 'normal', metadata: {} })
const form = reactive(blank())
function newKey() { return globalThis.crypto?.randomUUID?.() || `draft-${Date.now()}-${Math.random().toString(36).slice(2)}` }
let createKey = newKey()
const transitions = { open: ['in_progress', 'cancelled'], in_progress: ['resolved', 'cancelled'], resolved: ['closed', 'in_progress'] }
const canManage = computed(() => ['support', 'admin'].includes(session.value?.role))
function canonical(value) { return JSON.stringify(value, Object.keys(value).sort()) }
const dirty = computed(() => selected.value && (
  ['title', 'description', 'category', 'priority'].some(key => form[key] !== selected.value.payload[key]) ||
  canonical(form.metadata) !== canonical(selected.value.payload.metadata)))
function statusLabel(value) { return ({ waiting_confirmation: '待确认', completed: '已提交', cancelled: '已取消', rejected: '已拒绝', expired: '已过期', open: '待受理', in_progress: '处理中', resolved: '已解决', closed: '已关闭' })[value] || value }
function date(value) { return value ? new Date(value * 1000).toLocaleString() : '-' }
function clear() { selected.value = null; events.value = []; lastTicket.value = null; Object.assign(form, blank()); createKey = newKey() }
function newDraft() { clear() }
async function handle(work) {
  error.value = ''; pending.value = true
  try { return await work() } catch (failure) { error.value = failure.message; if (failure.status === 401) { session.value = null; taskList.value = []; ticketList.value = []; clear(); emit('session-change', null) } }
  finally { pending.value = false }
}
async function refresh() {
  if (!session.value) return
  const [taskData, ticketData] = await Promise.all([api.tasks(props.settings), api.tickets(props.settings)])
  taskList.value = taskData.items; ticketList.value = ticketData.items
}
async function signIn() {
  await handle(async () => {
    const result = await api.login(props.settings, email.value, password.value)
    password.value = ''; session.value = result
    emit('session-change', result); clear(); await refresh()
  })
}
async function signOut() {
  await handle(async () => { await api.logout(props.settings); session.value = null; taskList.value = []; ticketList.value = []; clear(); emit('session-change', null) })
}
async function select(task) {
  selected.value = task; lastTicket.value = null
  Object.assign(form, JSON.parse(JSON.stringify(task.payload)))
  await handle(async () => { events.value = (await api.taskEvents(props.settings, task)).items })
}
async function save() {
  await handle(async () => {
    const payload = JSON.parse(JSON.stringify(form))
    const task = selected.value ? await api.reviseDraft(props.settings, selected.value, payload) : await api.createDraft(props.settings, payload, createKey)
    selected.value = task; Object.assign(form, JSON.parse(JSON.stringify(task.payload)))
    await refresh(); emit('notice', '草稿已保存，请核对后确认')
  })
}
async function confirm() {
  await handle(async () => {
    const result = await api.confirmDraft(props.settings, selected.value)
    selected.value = result.task; lastTicket.value = result.ticket; await refresh()
    events.value = (await api.taskEvents(props.settings, selected.value)).items
  })
}
async function cancel() { await handle(async () => { selected.value = await api.cancelTask(props.settings, selected.value); await refresh() }) }
async function changeStatus(ticket, value) { await handle(async () => { await api.updateTicket(props.settings, ticket, value); await refresh() }) }
onMounted(async () => {
  try { session.value = await api.sessionInfo(props.settings); emit('session-change', session.value); await refresh() }
  catch { session.value = null; emit('session-change', null) }
})
watch(() => [props.visible, props.refreshKey], () => { if (session.value) handle(refresh) })
</script>
