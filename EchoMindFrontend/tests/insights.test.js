import test from 'node:test'
import assert from 'node:assert/strict'
import { safeSourceUrl, metricText, feedbackPayload, diagnosticMarkdown, fetchAnswers, fetchAnswer, saveFeedback } from '../src/lib/insights.js'

test('source links allow only credential-free HTTP/S', () => {
  for (const value of ['javascript:alert(1)', 'file:///tmp/secret', 'data:text/html,hi', 'https://user:secret@example.invalid', '', undefined]) assert.equal(safeSourceUrl(value), '')
  assert.equal(safeSourceUrl('https://example.invalid/manual'), 'https://example.invalid/manual')
})
test('metrics preserve unknown denominators instead of displaying zero or perfect success', () => {
  assert.equal(metricText({ numerator: 0, denominator: 0, value: null }), '暂无有效样本（0/0）')
  assert.equal(metricText({ numerator: 1, denominator: 2, value: .5 }), '50.0%（1/2）')
})
test('editing requires an actual changed reply and a reason; outcome defaults unknown', () => {
  const form = { decision: 'edited', reason: 'incomplete', comment: '', final_answer: '只能手洗。' }
  assert.throws(() => feedbackPayload({ ...form, decision: '' }, 0, 'a'), /请选择采纳方式/)
  assert.throws(() => feedbackPayload(form, 1, form.final_answer), /实际修改/)
  const payload = feedbackPayload({ ...form, final_answer: '只能手洗，自然晾干。' }, 1, form.final_answer)
  assert.equal(payload.revision, 1); assert.equal(payload.outcome, 'unknown')
  assert.throws(() => feedbackPayload({ ...form, reason: 'none' }, 1, ''), /原因/)
  assert.equal(feedbackPayload({ ...form, decision: 'accepted' }, 0, 'a').final_answer, '')
})
test('feedback transport cannot submit a forged original answer or sources', async () => {
  const originalFetch = globalThis.fetch, calls = []
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ revision: 1 }) } }
  try {
    const settings = { endpoints: { python: '/api/python' } }
    const controller = new AbortController()
    await fetchAnswers(settings, { limit: 20, offset: 20, signal: controller.signal })
    await fetchAnswer(settings, 'a/b', controller.signal)
    await saveFeedback(settings, 'a/b', { decision: 'accepted', revision: 0 }, controller.signal)
    assert.ok(calls[0].url.includes('offset=20'))
    assert.ok(calls[1].url.endsWith('/answers/a%2Fb'))
    assert.equal(calls[2].options.method, 'PUT'); assert.equal(calls[2].options.credentials, 'include')
    assert.equal(calls[2].options.signal, controller.signal)
    assert.deepEqual(JSON.parse(calls[2].options.body), { decision: 'accepted', revision: 0 })
  } finally { globalThis.fetch = originalFetch }
})
test('report export retains sample sizes, unknown outcomes, window and caveats', () => {
  const unknown = { numerator: 0, denominator: 0, value: null }
  const report = { id: 'r1', window: { start_at: 1000, end_at: 2000 }, feedback_as_of: 2001,
    counts: { answers: 2, reviewed: 0, unreviewed: 2, outcome_unknown: 2 },
    metrics: { review_coverage: { numerator: 0, denominator: 2, value: 0 }, acceptance_rate: unknown, edit_rate: unknown, discard_rate: unknown, resolution_rate: unknown },
    issue_examples: [], recommendations: ['核实 <script>bad</script>'], limitations: ['不是系统成功率'] }
  const markdown = diagnosticMarkdown(report)
  assert.ok(markdown.includes('已保存回答 2 条'))
  assert.ok(markdown.includes('未知：2 条'))
  assert.ok(markdown.includes('左闭右开')); assert.ok(markdown.includes('不是系统成功率'))
  assert.ok(!markdown.includes('<script>'))
})
