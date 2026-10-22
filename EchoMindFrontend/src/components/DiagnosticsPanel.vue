<template>
  <section class="page page-diagnostics">
    <div class="page-heading"><div class="heading-copy"><span class="kicker">Service improvement</span><h1>{{ historyOnly ? (admin ? '咨询与反馈' : '我的咨询') : '客服诊断' }}</h1><p>{{ historyOnly ? '查看已保存的回答、参考来源与采纳反馈。' : '复核真实咨询、采纳反馈与知识缺口，保存可追溯的阶段报告。' }}</p></div><button @click="refresh" :disabled="busy">刷新</button></div>
    <p v-if="error" class="danger" role="alert">{{ error }}</p>
    <template v-if="canRead">
      <nav v-if="!historyOnly" class="admin-tabs" aria-label="客服诊断内容"><button :class="{ active: tab === 'reports' }" @click="tab = 'reports'">诊断报告</button><button :class="{ active: tab === 'answers' }" @click="tab = 'answers'">咨询与反馈</button></nav>
      <section v-if="tab === 'reports'" class="workspace-card diagnostic-controls">
        <div><h2>生成阶段诊断</h2><p class="muted">按咨询产生时间统计，汇总每条回答截至生成时的最新反馈。报告保存后保持不变。</p></div>
        <label>时间范围 <select v-model.number="days"><option :value="7">最近 7 天</option><option :value="14">最近 14 天</option><option :value="30">最近 30 天</option></select></label><button :disabled="busy" @click="generate">{{ busy ? '处理中…' : '生成并保存报告' }}</button>
      </section>
      <p v-if="busy" class="muted" role="status">正在读取本组织数据…</p>
      <section class="workspace-card">
        <template v-if="tab === 'reports'">
          <h2>历史诊断报告</h2>
          <div v-for="item in reports.items" :key="item.id" class="document-row"><button class="quiet-button" :disabled="busy" @click="selectReport(item.id)">{{ date(item.created_at) }} · {{ item.counts.answers }} 条回答 · {{ item.counts.reviewed }} 条反馈</button><small>{{ date(item.start_at) }} 至 {{ date(item.end_at) }}</small></div>
          <p v-if="!reports.items.length" class="workspace-empty">还没有诊断报告。可以先收集咨询与反馈，再生成报告；空报告会明确显示无样本。</p>
        </template>
        <template v-else>
          <h2>{{ admin || session?.role === 'support' ? '本组织咨询与反馈' : '我的咨询与反馈' }}</h2>
          <article v-for="item in answers.items" :key="item.request_id" class="admin-record"><button class="quiet-button" :disabled="busy" @click="selectAnswer(item.request_id)">{{ item.question }}</button><small>{{ date(item.created_at) }} · {{ item.feedback ? decisionLabels[item.feedback.decision] + ' / ' + outcomeLabels[item.feedback.outcome] : '尚未反馈' }} · {{ item.sources.length }} 个来源</small><p class="muted">{{ item.response.slice(0, 180) }}</p></article>
          <p v-if="!answers.items.length" class="workspace-empty">还没有保存的咨询。升级前的聊天不会自动补入，登录后新完成的聊天会保存来源快照。</p>
        </template>
        <div class="admin-pagination"><span>共 {{ page.total }} 条</span><button class="quiet-button" :disabled="busy || page.offset === 0" @click="move(-1)">上一页</button><button class="quiet-button" :disabled="busy || page.offset + page.items.length >= page.total" @click="move(1)">下一页</button></div>
      </section>

      <section v-if="report && tab === 'reports'" class="diagnostic-detail">
        <div class="card-heading"><div><h2>诊断结果</h2><small>{{ date(report.window.start_at) }} 至 {{ date(report.window.end_at) }}（左闭右开）· 反馈快照 {{ date(report.feedback_as_of) }}</small></div><button class="quiet-button" @click="download">导出 Markdown</button></div>
        <p class="muted diagnostic-sample-note">以下为人工反馈结果；请结合分母判断，小样本只用于发现问题。未反馈或未确认解决的情况保留为未知。</p>
        <div class="diagnostic-metrics"><article v-for="metric in metrics" :key="metric.key" class="workspace-card"><span>{{ metric.label }}</span><strong>{{ metricText(report.metrics[metric.key]) }}</strong><small>{{ metric.description }}</small></article></div>
        <section class="workspace-card"><h2>样本与处理情况</h2><p>已保存回答 {{ report.counts.answers }} 条 · 未反馈 {{ report.counts.unreviewed }} 条 · 解决结果未知 {{ report.counts.outcome_unknown }} 条</p><p>转人工 {{ report.counts.escalated }} 条 · 有检索警告 {{ report.counts.with_warnings }} 条 · 无来源 {{ report.counts.without_evidence }} 条</p><p>服务响应平均 {{ duration(report.duration_ms.mean) }} · P95 {{ duration(report.duration_ms.p95) }}（{{ report.duration_ms.count }} 条，不含人工审核）</p></section>
        <div class="diagnostic-columns">
          <section class="workspace-card"><h2>反馈问题分布</h2><p v-for="(count, reason) in report.issue_distribution" :key="reason">{{ reasonLabels[reason] }}：{{ count }} 条</p><p v-if="!Object.keys(report.issue_distribution).length" class="muted">没有已标记的问题，不能据此认定全部回答正确。</p></section>
          <section class="workspace-card"><h2>改进建议</h2><p v-for="(item, index) in report.recommendations" :key="index">{{ item }}</p><p v-if="!report.recommendations.length" class="muted">暂无额外建议，请继续复核样本。</p></section>
        </div>
        <section class="workspace-card"><h2>待处理案例</h2><p class="muted">点击查看咨询的当前反馈。报告中的统计及反馈版本为生成时快照。</p><article v-for="item in report.issue_examples" :key="item.request_id" class="admin-record"><button class="quiet-button" @click="selectAnswer(item.request_id)" :disabled="busy">{{ item.question }}</button><small>{{ reasonLabels[item.reason] || '未标记原因' }} · {{ outcomeLabels[item.outcome] }} · 反馈版本 {{ item.feedback_revision }}</small></article><p v-if="!report.issue_examples.length" class="muted">暂无已标记的问题案例。</p></section>
        <section v-if="report.review_queue.length" class="workspace-card"><h2>优先复核的未反馈案例</h2><p class="muted">来自无来源、检索警告或转人工的未反馈回答，不自动认定为错误。</p><p v-for="item in report.review_queue" :key="item.request_id"><button class="quiet-button" @click="selectAnswer(item.request_id)" :disabled="busy">{{ item.question }}</button></p></section>
        <div class="diagnostic-columns"><section class="workspace-card"><h2>高频问题</h2><p class="muted">原文空白归一后精确计数，未做语义聚类。</p><p v-for="item in report.frequent_questions" :key="item.question">{{ item.question }} · {{ item.count }} 次</p></section><section class="workspace-card"><h2>来源使用情况</h2><p class="muted">按回答去重统计，不代表引用准确率。</p><p v-for="item in report.source_usage" :key="item.document_id + item.document_version">{{ item.title || item.document_id }} · {{ item.answer_count }} 条回答<small>文档版本 {{ item.document_version || '未提供' }}</small></p></section></div>
        <section class="workspace-card"><h2>统计边界</h2><p v-for="(item, index) in report.limitations" :key="index" class="muted">{{ item }}</p></section>
      </section>

      <section v-if="selected" class="workspace-card selected-answer">
        <div class="card-heading"><h2>咨询复核</h2><button class="quiet-button" @click="selected = null">关闭复核</button></div><small>{{ selected.request_id }} · {{ date(selected.created_at) }}</small><h3>{{ selected.question }}</h3><p class="answer-original">{{ selected.response }}</p>
        <AnswerEvidence :sources="selected.sources" /><p v-for="warning in selected.warnings" :key="warning" class="danger">{{ warning }}</p>
        <AnswerFeedback :key="selected.request_id" :settings="settings" :session="session" :request-id="selected.request_id" :answer="selected.response" @saved="feedbackSaved" @notice="emit('notice', $event)" @session-expired="emit('session-expired')" />
      </section>
    </template>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { identityKey } from '../lib/access.js'
import { isAdmin } from '../lib/admin.js'
import { fetchAnswers, fetchAnswer, fetchDiagnostics, fetchDiagnostic, createDiagnostic, diagnosticMarkdown, metricText, decisionLabels, reasonLabels, outcomeLabels } from '../lib/insights.js'
import AnswerEvidence from './AnswerEvidence.vue'
import AnswerFeedback from './AnswerFeedback.vue'

const props = defineProps({ settings: { type: Object, required: true }, session: { type: Object, default: null }, historyOnly: { type: Boolean, default: false } })
const emit = defineEmits(['notice', 'session-expired'])
const empty = () => ({ items: [], total: 0, limit: 20, offset: 0 })
const answers = ref(empty()), reports = ref(empty()), report = ref(null), selected = ref(null)
const days = ref(7), tab = ref('reports'), busy = ref(false), error = ref('')
const admin = computed(() => isAdmin(props.session)), page = computed(() => tab.value === 'reports' ? reports.value : answers.value)
const canRead = computed(() => admin.value || (props.historyOnly && Boolean(props.session?.organization_id && (props.session?.user_id || props.session?.user?.id))))
const metrics = [
  { key: 'review_coverage', label: '反馈覆盖率', description: '已反馈回答 / 全部已保存回答' },
  { key: 'acceptance_rate', label: '直接采纳率', description: '直接采纳 / 已反馈回答' },
  { key: 'edit_rate', label: '修改采纳率', description: '修改后采纳 / 已反馈回答' },
  { key: 'discard_rate', label: '弃用率', description: '弃用 / 已反馈回答' },
  { key: 'resolution_rate', label: '已知结果中的解决率', description: '已解决 / 已解决与未解决，未知不计入' }
]
let generation = 0, controller
const scope = () => [identityKey(props.session), props.settings.endpoints?.python, props.historyOnly].join('|')
function reset() {
  generation += 1; controller?.abort(); answers.value = empty(); reports.value = empty()
  report.value = null; selected.value = null; busy.value = false; error.value = ''; tab.value = props.historyOnly ? 'answers' : 'reports'
}
async function run(work) {
  if (!canRead.value) return
  controller?.abort(); controller = new AbortController()
  const signal = controller.signal, version = ++generation, identity = scope()
  const current = () => version === generation && canRead.value && identity === scope()
  busy.value = true; error.value = ''
  try { await work(signal, current) }
  catch (failure) {
    if (!current() || failure.name === 'AbortError') return
    if ([401, 403].includes(failure.status)) { reset(); emit('session-expired'); return }
    error.value = failure.message
  } finally { if (current()) busy.value = false }
}
async function refresh() {
  await run(async (signal, current) => {
    if (props.historyOnly) {
      const value = await fetchAnswers(props.settings, { signal })
      if (current()) answers.value = value
      return
    }
    const [a, r] = await Promise.all([fetchAnswers(props.settings, { signal }), fetchDiagnostics(props.settings, { signal })])
    if (current()) { answers.value = a; reports.value = r }
  })
}
async function generate() {
  await run(async (signal, current) => {
    const value = await createDiagnostic(props.settings, days.value, signal)
    const history = await fetchDiagnostics(props.settings, { signal })
    if (current()) { report.value = value; reports.value = history; emit('notice', '诊断报告已保存') }
  })
}
async function selectReport(id) {
  await run(async (signal, current) => { const value = await fetchDiagnostic(props.settings, id, signal); if (current()) report.value = value })
}
async function selectAnswer(id) {
  await run(async (signal, current) => {
    const value = await fetchAnswer(props.settings, id, signal)
    if (current()) selected.value = value
  })
}
async function move(direction) {
  const kind = tab.value, data = page.value
  await run(async (signal, current) => {
    const value = await (kind === 'reports' ? fetchDiagnostics : fetchAnswers)(props.settings, { signal, limit: data.limit, offset: Math.max(0, data.offset + direction * data.limit) })
    if (current()) (kind === 'reports' ? reports : answers).value = value
  })
}
function feedbackSaved(feedback) {
  if (!selected.value) return
  selected.value.feedback = feedback
  const row = answers.value.items.find(item => item.request_id === selected.value.request_id)
  if (row) row.feedback = feedback
}
function download() {
  if (!report.value || !admin.value) return
  const url = URL.createObjectURL(new Blob([diagnosticMarkdown(report.value)], { type: 'text/markdown;charset=utf-8' }))
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `echomind-diagnostic-${report.value.id.slice(0, 8)}.md`; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function date(value) { return new Date(value * 1000).toLocaleString() }
function duration(value) { return value == null ? '无样本' : `${(value / 1000).toFixed(2)} 秒` }
watch(() => [identityKey(props.session), props.settings.endpoints?.python, props.historyOnly], () => { reset(); if (canRead.value) refresh() }, { immediate: true })
onBeforeUnmount(() => { generation += 1; controller?.abort() })
</script>
