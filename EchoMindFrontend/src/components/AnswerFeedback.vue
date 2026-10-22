<template>
  <section class="answer-feedback">
    <div class="feedback-toolbar">
      <button type="button" class="quiet-button" @click="copy(original)">复制回复</button>
      <button type="button" class="quiet-button" @click="open = !open; open && load()" :disabled="busy">{{ open ? '收起反馈' : saved ? '查看 / 修改反馈' : '记录采纳与处理结果' }}</button>
      <span v-if="saved" class="muted">{{ decisionLabels[saved.decision] }} · {{ outcomeLabels[saved.outcome] }}</span>
    </div>
    <p v-if="error" class="danger" role="alert">{{ error }}</p>
    <form v-if="open && loaded" class="feedback-form" @submit.prevent="save">
      <p class="muted">复制不会自动记为采纳。请按实际审核与处理情况填写，尚未确认的解决结果保留为未知。</p>
      <fieldset :disabled="busy">
        <legend>这条建议如何使用？</legend>
        <div class="feedback-choices"><label v-for="(label, key) in decisionLabels" :key="key"><input v-model="form.decision" type="radio" :value="key" />{{ label }}</label></div>
        <label v-if="form.decision && form.decision !== 'accepted'"><span>修改或弃用原因</span><select v-model="form.reason"><option value="none" disabled>请选择原因</option><option v-for="(label, key) in editableReasons" :key="key" :value="key">{{ label }}</option></select></label>
        <label v-if="form.decision === 'edited'"><span>实际采用的回复</span><textarea v-model="form.final_answer" rows="5" maxlength="100000" /></label>
        <label><span>顾客的问题是否解决？</span><select v-model="form.outcome"><option v-for="(label, key) in outcomeLabels" :key="key" :value="key">{{ label }}</option></select></label>
        <label><span>补充说明</span><textarea v-model="form.comment" rows="2" maxlength="2000" placeholder="例如遗漏了哪个条件，或实际如何处理" /></label>
        <div class="feedback-toolbar"><button type="submit" :disabled="conflict">{{ busy ? '保存中…' : '保存反馈' }}</button><button v-if="form.decision === 'edited'" type="button" class="quiet-button" @click="copy(form.final_answer)">复制修改后的回复</button><button v-if="conflict" type="button" class="quiet-button" @click="load">载入最新反馈（替换当前编辑）</button></div>
      </fieldset>
      <small v-if="saved" class="muted">反馈版本 {{ saved.revision }} · {{ new Date(saved.updated_at * 1000).toLocaleString() }}。修改记录保存在服务端。</small>
    </form>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { identityKey } from '../lib/access.js'
import { decisionLabels, reasonLabels, outcomeLabels, fetchAnswer, saveFeedback, feedbackPayload } from '../lib/insights.js'

const props = defineProps({ settings: { type: Object, required: true }, session: { type: Object, required: true }, requestId: { type: String, required: true }, answer: { type: String, default: '' } })
const emit = defineEmits(['saved', 'notice', 'session-expired'])
const open = ref(false), loaded = ref(false), busy = ref(false), error = ref(''), saved = ref(null), conflict = ref(false), original = ref(props.answer)
const form = reactive({ decision: '', reason: 'none', outcome: 'unknown', comment: '', final_answer: props.answer })
const editableReasons = computed(() => Object.fromEntries(Object.entries(reasonLabels).filter(([key]) => key !== 'none')))
let generation = 0, controller
const scope = () => [identityKey(props.session), props.requestId, props.settings.endpoints?.python].join('|')
function reset() {
  generation += 1; controller?.abort(); busy.value = false; error.value = ''; loaded.value = false; open.value = false
  saved.value = null; conflict.value = false; original.value = props.answer
  Object.assign(form, { decision: '', reason: 'none', outcome: 'unknown', comment: '', final_answer: props.answer })
}
async function run(work) {
  controller?.abort(); controller = new AbortController()
  const signal = controller.signal, version = ++generation, identity = scope()
  const current = () => version === generation && identity === scope() && Boolean(identityKey(props.session))
  busy.value = true; error.value = ''
  try { await work(signal, current) }
  catch (failure) {
    if (!current() || failure.name === 'AbortError') return
    if ([401, 403].includes(failure.status)) { reset(); emit('session-expired'); return }
    conflict.value = failure.status === 409
    error.value = failure.status === 409 ? '反馈已被更新；当前编辑已保留，请先重新载入最新反馈。' : failure.message
  } finally { if (current()) busy.value = false }
}
async function load() {
  await run(async (signal, current) => {
    const answer = await fetchAnswer(props.settings, props.requestId, signal)
    if (!current()) return
    original.value = answer.response; saved.value = answer.feedback; loaded.value = true; conflict.value = false
    Object.assign(form, { decision: '', reason: 'none', outcome: 'unknown', comment: '', final_answer: answer.response }, answer.feedback || {})
    if (!form.final_answer) form.final_answer = answer.response
  })
}
async function save() {
  if (conflict.value) return
  await run(async (signal, current) => {
    const payload = feedbackPayload(form, saved.value?.revision || 0, original.value)
    const feedback = await saveFeedback(props.settings, props.requestId, payload, signal)
    if (!current()) return
    saved.value = feedback; emit('saved', feedback); emit('notice', '反馈已保存')
  })
}
async function copy(value) {
  const version = generation
  try { await navigator.clipboard.writeText(value); if (version === generation) emit('notice', '已复制，请审核后发送') }
  catch { if (version === generation) error.value = '当前浏览器无法复制，请选中文字手动复制。' }
}
watch(() => [identityKey(props.session), props.requestId, props.settings.endpoints?.python], reset)
onBeforeUnmount(() => { generation += 1; controller?.abort() })
</script>
