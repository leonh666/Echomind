import test from 'node:test'
import assert from 'node:assert/strict'
import { chatStatus, replyMeta } from '../src/lib/chat-status.js'

test('mentioning human staff in prose does not change the handling status', () => {
  for (const response of ['请联系人工客服', '不用转人工', '退款政策']) {
    assert.equal(chatStatus({ response, escalated: false }).label, 'AI 答复')
  }
})

test('recommendations and saved drafts never claim delivery to human staff', () => {
  const recommendation = chatStatus({ escalated: true, escalationReasons: ['紧急问题'] })
  assert.equal(recommendation.label, '建议人工核验')
  assert.match(recommendation.detail, /紧急问题.*尚未转交人工/)
  const draft = chatStatus({ escalated: true, tasks: [{ status: 'waiting_confirmation' }] })
  assert.equal(draft.label, '待确认工单')
  assert.match(draft.detail, /尚未提交/)
  assert.equal(replyMeta({ primaryAgent: 'billing', escalated: false }, true), '账单服务 · AI 答复')
  assert.equal(replyMeta({ escalated: true }), '建议人工核验')
})
