import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { identityKey, workspaceAccess } from '../src/lib/access.js'

// Test the actual setup script with reactive/API doubles, without Vue or a browser.
const setup = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8').split('<script setup>')[1].split('</script>')[0]
let importing = false
const script = setup.split('\n').filter(line => {
  if (/^import\s/.test(line)) importing = true
  if (!importing) return true
  if (/\bfrom\s+['"]/.test(line)) importing = false
  return false
}).join('\n')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const member = (id, role = 'customer', organization_id = 'org1') => ({ user_id: id, role, organization_id, user: { id } })
const settle = () => new Promise(resolve => setImmediate(resolve))
function workspace(overrides = {}) {
  const calls = [], clearedTimers = []
  let sequence = 0
  const api = (name, result) => async () => { calls.push(name); return result }
  const bindings = {
    ref: value => ({ value }), reactive: value => value, computed: get => ({ get value() { return get() } }),
    watch() {}, onMounted() {}, onBeforeUnmount() {}, nextTick: async () => {}, identityKey, workspaceAccess,
    availableBackendIds: () => ['python'], createInitialSettings: () => ({ backend: 'python', endpoints: { python: '/api/python' }, userId: '', conversationId: '' }),
    backendMeta: () => ({ label: 'Python', baseUrl: '/api/python' }), saveSettings() {},
    requestHealth: api('health', { status: 'ok' }), requestKnowledgeStats: api('stats', { total_chunks: 2 }),
    requestMonitor: api('monitor', { agent_stats: {}, active_alerts: [] }), requestSkills: api('skills', { skills: [] }),
    evaluationReports: api('reports', { items: [] }), promoteEvaluation: api('promote', {}), reloadSkills: api('reload', { skills: [] }),
    requestSearch: api('search', { results: [] }), requestToolTrace: api('trace', null), requestEvaluation: api('eval', {}),
    addKnowledge: api('add', {}), uploadKnowledge: api('upload', {}), requestChatStream: api('chat', { response: 'ok' }),
    requestAnimationFrame() {}, setInterval: () => ++sequence, clearInterval: id => clearedTimers.push(id),
    setTimeout: () => ++sequence, clearTimeout() {}, ...overrides
  }
  const exposed = 'sessionChanged,refreshConsole,loadSkills,loadReports,loadMonitor,reloadSkillSet,sendMessage,authSession,activeView,draft,docTitle,docContent,docSource,docVersion,searchQuery,searchResults,reports,evalData,messages,skillsData,monitorData,busy,chatPending'
  return { ...new Function(...Object.keys(bindings), `${script}\nreturn {${exposed}, chatState: () => ({ controller: chatController, timer: chatTimer })}`)(...Object.values(bindings)), calls, clearedTimers }
}
test('identity changes clear personal drafts, backend forms and cached records', async () => {
  const app = workspace(); app.sessionChanged(member('admin', 'admin')); await settle()
  const fields = ['draft', 'docTitle', 'docContent', 'docSource', 'docVersion', 'searchQuery']
  for (const key of fields) app[key].value = 'old private text'
  app.reports.value = [{ private: true }]; app.messages.value = [{ content: 'old reply' }]; app.activeView.value = 'admin'
  app.sessionChanged(member('alice')); await settle()
  for (const key of fields) assert.equal(app[key].value, '')
  assert.deepEqual(app.reports.value, []); assert.deepEqual(app.messages.value, []); assert.equal(app.activeView.value, 'chat')
})
test('same user changing organization or role invalidates private state', async () => {
  const app = workspace(); app.sessionChanged(member('same', 'admin')); await settle()
  app.draft.value = 'org1'; app.sessionChanged(member('same', 'admin', 'org2')); assert.equal(app.draft.value, '')
  app.draft.value = 'admin'; app.sessionChanged(member('same', 'customer', 'org2')); assert.equal(app.draft.value, ''); await settle()
})
test('customer console refresh sends no backend requests', async () => {
  const app = workspace(); app.sessionChanged(member('alice')); await app.refreshConsole()
  assert.ok(app.calls.length > 0); assert.ok(app.calls.every(name => name === 'health'))
})
test('late admin responses cannot refill private data after logout', async () => {
  const response = deferred(), app = workspace({ requestSkills: () => response.promise, evaluationReports: () => response.promise, requestMonitor: () => response.promise })
  app.sessionChanged(member('admin', 'admin'))
  const pending = Promise.all([app.loadSkills(), app.loadReports(), app.loadMonitor()])
  app.sessionChanged(null); response.resolve({ items: [{ private: true }], skills: [{ private: true }], agent_stats: { private: true } }); await pending
  assert.deepEqual(app.reports.value, []); assert.deepEqual(app.skillsData.value.skills, []); assert.deepEqual(app.monitorData.value.agent_stats, {})
})
test('401 and 403 clear the global session and private state', async () => {
  for (const status of [401, 403]) {
    const app = workspace({ reloadSkills: async () => { throw Object.assign(new Error('Access revoked'), { status }) } })
    app.sessionChanged(member('admin', 'admin')); await settle(); app.draft.value = 'private'
    await app.reloadSkillSet(); assert.equal(app.authSession.value, null); assert.equal(app.draft.value, ''); assert.equal(app.busy.value, false)
  }
})
test('old chat cleanup cannot stop a new user chat after identity changes', async () => {
  const old = deferred(), fresh = deferred(); let trial = 0
  const app = workspace({ requestChatStream: () => (++trial === 1 ? old.promise : fresh.promise) })
  app.sessionChanged(member('alice')); app.draft.value = 'old message'; const oldRun = app.sendMessage(), oldTimer = app.chatState().timer
  app.sessionChanged(null); app.sessionChanged(member('bob')); app.draft.value = 'new message'; const newRun = app.sendMessage(), current = app.chatState()
  old.resolve({ response: 'old reply' }); await oldRun
  assert.equal(app.chatPending.value, true); assert.equal(app.busy.value, true); assert.deepEqual(app.chatState(), current)
  assert.ok(app.clearedTimers.includes(oldTimer)); assert.ok(!app.clearedTimers.includes(current.timer))
  fresh.resolve({ response: 'new reply' }); await newRun
  assert.equal(app.chatPending.value, false); assert.ok(!JSON.stringify(app.messages.value).includes('old reply'))
})
