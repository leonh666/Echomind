import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { identityKey } from '../src/lib/access.js'
import { isAdmin } from '../src/lib/admin.js'
import { feedbackPayload } from '../src/lib/insights.js'

const member = (id, role = 'customer') => ({ user_id: id, organization_id: 'org1', role })
const defer = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
function setup(name, props, overrides, exposed) {
  const content = readFileSync(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8').split('<script setup>')[1].split('</script>')[0]
  const script = content.split('\n').filter(line => !line.startsWith('import ')).join('\n')
  const emitted = [], watchers = []
  const bindings = {
    defineProps: () => props, defineEmits: () => (...args) => emitted.push(args),
    ref: value => ({ value }), reactive: value => value, computed: get => ({ get value() { return get() } }),
    watch: (get, callback) => watchers.push(callback), onBeforeUnmount() {}, identityKey, isAdmin,
    decisionLabels: { accepted: '采纳', edited: '修改', discarded: '弃用' }, reasonLabels: {}, outcomeLabels: {}, feedbackPayload,
    fetchAnswer: async () => ({ response: '原文', feedback: null }), saveFeedback: async () => ({ revision: 1 }),
    fetchAnswers: async () => ({ items: [], total: 0 }), fetchDiagnostics: async () => ({ items: [], total: 0 }),
    fetchDiagnostic: async () => ({}), createDiagnostic: async () => ({}), ...overrides
  }
  return { ...new Function(...Object.keys(bindings), `${script}\nreturn {${exposed}}`)(...Object.values(bindings)), emitted, watchers }
}
test('late answer load cannot refill feedback state after account switches', async () => {
  const pending = defer(), props = { settings: { endpoints: { python: '/api' } }, session: member('a'), requestId: 'r1', answer: 'a' }
  const component = setup('AnswerFeedback', props, { fetchAnswer: () => pending.promise }, 'load,reset,saved,original,loaded')
  const loading = component.load(); props.session = member('b')
  // Even before the Vue watcher runs, the captured request scope must reject the response.
  pending.resolve({ response: 'old private answer', feedback: { comment: 'private' } }); await loading
  assert.equal(component.loaded.value, false); assert.equal(component.saved.value, null)
  component.reset(); assert.equal(component.original.value, 'a')
})
test('a feedback conflict preserves unsaved edits and requires reloading', async () => {
  const props = { settings: { endpoints: { python: '/api' } }, session: member('a'), requestId: 'r1', answer: '原文' }
  const component = setup('AnswerFeedback', props, { saveFeedback: async () => { throw Object.assign(new Error('conflict'), { status: 409 }) } }, 'save,form,conflict,error')
  Object.assign(component.form, { decision: 'edited', reason: 'incomplete', final_answer: '补充后的回复' })
  await component.save()
  assert.equal(component.conflict.value, true); assert.equal(component.form.final_answer, '补充后的回复')
  assert.match(component.error.value, /已保留/)
})
test('customer answer history never loads organization diagnostic reports', async () => {
  const props = { settings: { endpoints: { python: '/api' } }, session: member('a'), historyOnly: true }
  let reportCalls = 0
  const component = setup('DiagnosticsPanel', props, { fetchDiagnostics: async () => { reportCalls += 1; return {} } }, 'refresh,answers')
  await component.refresh(); assert.equal(reportCalls, 0)
})
test('late organization report data is rejected when role changes before watcher runs', async () => {
  const pending = defer(), props = { settings: { endpoints: { python: '/api' } }, session: member('a', 'admin'), historyOnly: false }
  const component = setup('DiagnosticsPanel', props, { fetchDiagnostics: () => pending.promise }, 'refresh,reports,reset')
  const loading = component.refresh(); props.session = member('a', 'customer')
  pending.resolve({ items: [{ id: 'private-report' }], total: 1 }); await loading
  assert.deepEqual(component.reports.value.items, [])
})
