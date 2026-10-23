import test from 'node:test'
import assert from 'node:assert/strict'
import { login, confirmDraft, reviseDraft, createDraft, feishuInfo, checkFeishu, retryFeishu } from '../src/lib/operations.js'
import { requestSearch, requestChat } from '../src/lib/backends.js'

const settings = { endpoints: { python: '/api/python' } }

test('Feishu requests use the session and server configured destination', async context => {
  const requests = []
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push([url, options.method, options.body])
    assert.equal(options.credentials, 'include')
    return Response.json({ enabled: true })
  })
  await feishuInfo(settings); await checkFeishu(settings); await retryFeishu(settings, { id: 'ticket/1', app_token: 'forged' })
  assert.deepEqual(requests, [
    ['/api/python/integrations/feishu', 'GET', undefined],
    ['/api/python/integrations/feishu/check', 'POST', '{}'],
    ['/api/python/tickets/ticket%2F1/feishu/retry', 'POST', '{}']
  ])
})

test('confirmation sends the exact saved revision, hash and idempotency key with session cookie', async context => {
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/python/tasks/task-1/confirm')
    assert.equal(options.credentials, 'include')
    assert.deepEqual(JSON.parse(options.body), { revision: 3, payload_hash: 'sha256-body', idempotency_key: 'key-1' })
    return Response.json({ ticket: { id: 'ticket-1' } })
  })
  const result = await confirmDraft(settings, { id: 'task-1', revision: 3, payload_hash: 'sha256-body', idempotency_key: 'key-1', payload: { title: 'unsent edit' } })
  assert.equal(result.ticket.id, 'ticket-1')
})

test('revision conflicts preserve a structured error for the UI', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code: 'revision_conflict', message: '草稿已修改' } }, { status: 409 }))
  await assert.rejects(reviseDraft(settings, { id: 'a', revision: 1 }, { title: 'title' }), error => error.status === 409 && error.code === 'revision_conflict' && error.message === '草稿已修改')
})

test('login uses HttpOnly cookie transport without asking the browser to persist tokens', async context => {
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/python/auth/login')
    assert.deepEqual(JSON.parse(options.body), { email: 'test@example.invalid', password: 'password' })
    assert.equal(options.credentials, 'include')
    return Response.json({ role: 'customer' })
  })
  assert.equal((await login(settings, 'test@example.invalid', 'password')).role, 'customer')
})

test('search sends explicit strategy and rewrite configuration', async context => {
  context.mock.method(globalThis, 'fetch', async url => {
    const parsed = new URL(url, 'http://localhost')
    assert.equal(parsed.searchParams.get('strategy'), 'hybrid')
    assert.equal(parsed.searchParams.get('rewrite'), 'false')
    assert.equal(parsed.searchParams.get('product_version'), 'v2')
    assert.equal(parsed.searchParams.get('knowledge_base_id'), 'support')
    return Response.json({ results: [] })
  })
  await requestSearch('python', settings, '退款', 5, 'hybrid', false, {product_version:' v2 ', knowledge_base_id:'support'})
})

test('chat keeps request evidence, pending tasks and failure warnings', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({ response: '答复', sources: [{ chunk_id: 'c1' }], tasks: [{ id: 't1' }], warnings: ['部分召回失败'] }))
  const result = await requestChat('python', settings, '问题')
  assert.equal(result.sources[0].chunk_id, 'c1')
  assert.equal(result.tasks[0].id, 't1')
  assert.deepEqual(result.warnings, ['部分召回失败'])
})
