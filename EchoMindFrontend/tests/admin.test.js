import test from 'node:test'
import assert from 'node:assert/strict'
import { AdminApiError, fetchAdminOverview, fetchAdminUsers, fetchAdminActivity, fetchAdminConversations, fetchAdminTasks, fetchAdminTickets } from '../src/lib/admin.js'

const settings = { endpoints: { python: '/api/python' } }
const admin = { user_id: 'admin-1', organization_id: 'org-1', role: 'admin' }
const page = (items, extra = {}) => ({ items, total: items.length, limit: 20, offset: 0, ...extra })

test('every admin endpoint rejects a non-admin session before any network request', async context => {
  let calls = 0
  context.mock.method(globalThis, 'fetch', async () => { calls += 1; throw new Error('must not fetch') })
  for (const session of [null, { role: 'customer' }, { role: 'support' }, { role: 'kb_admin' }]) {
    for (const load of [fetchAdminOverview, fetchAdminUsers, fetchAdminActivity, fetchAdminConversations, fetchAdminTasks, fetchAdminTickets]) {
      await assert.rejects(load(settings, session), error => error instanceof AdminApiError && error.status === 403 && error.code === 'admin_required')
    }
  }
  assert.equal(calls, 0)
})

test('users pagination sends limit and offset with HttpOnly session credentials and cancellation signal', async context => {
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    const parsed = new URL(url, 'https://example.invalid')
    assert.equal(parsed.pathname, '/api/python/admin/users')
    assert.equal(parsed.searchParams.get('limit'), '50')
    assert.equal(parsed.searchParams.get('offset'), '100')
    assert.equal(options.credentials, 'include')
    assert.equal(options.signal, controller.signal)
    return Response.json(page([{ id: 'u1', email: 'u@example.invalid', role: 'customer', password_hash: 'secret', token: 'secret' }], { total: 125, limit: 50, offset: 100 }))
  })
  const result = await fetchAdminUsers(settings, admin, { limit: 50, offset: 100, signal: controller.signal })
  assert.deepEqual(result, { items: [{ id: 'u1', email: 'u@example.invalid', memberships: [] }], total: 125, limit: 50, offset: 100 })
})

test('invalid pagination is rejected locally instead of sending bad or unbounded requests', async context => {
  let calls = 0
  context.mock.method(globalThis, 'fetch', async () => { calls += 1 })
  for (const options of [{ limit: 0 }, { limit: 101 }, { limit: 1.5 }, { offset: -1 }, { offset: Infinity }, { offset: '20' }]) {
    await assert.rejects(fetchAdminUsers(settings, admin, options), error => error.status === 400 && error.code === 'invalid_pagination')
  }
  assert.equal(calls, 0)
})

test('401 and 403 preserve structured errors for access revocation in the dashboard', async context => {
  for (const status of [401, 403]) {
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code: 'session_denied', message: '请重新登录' } }, { status }))
    await assert.rejects(fetchAdminUsers(settings, admin), error => error.status === status && error.code === 'session_denied' && error.message === '请重新登录')
  }
})

test('malformed JSON and pagination responses are structured failures', async context => {
  for (const value of [null, [], {}, page([], { total: '1' }), page([], { limit: 0 }), page([{}], { limit: 0 }), page([], { limit: 101 }), page([], { offset: 20 }), page([{}], { total: 0 })]) {
    context.mock.method(globalThis, 'fetch', async () => Response.json(value))
    await assert.rejects(fetchAdminUsers(settings, admin), error => error.status === 502 && error.code === 'invalid_response')
  }
  context.mock.method(globalThis, 'fetch', async () => new Response('<html>unexpected</html>', { status: 200 }))
  await assert.rejects(fetchAdminUsers(settings, admin), error => error.status === 502)
})

test('overview counts and status maps are allowlisted rather than exposing arbitrary fields', async context => {
  context.mock.method(globalThis, 'fetch', async url => {
    assert.equal(url, '/api/python/admin/overview')
    return Response.json({ counts: { users: 2, tasks: 3, tickets: 1, events: 9, conversations: 4, requests: 10, token: 'secret' }, task_status: { waiting_confirmation: 2, password: 'secret' }, ticket_status: { open: 1, payload_hash: 'secret' }, credentials: 'secret' })
  })
  const result = await fetchAdminOverview(settings, admin)
  assert.deepEqual(result.task_status, { waiting_confirmation: 2 })
  assert.deepEqual(result.ticket_status, { open: 1 })
  assert.equal(result.counts.requests, 10)
  assert.equal(JSON.stringify(result).includes('secret'), false)
})

test('global user directory retains usernames and safe per-organization memberships', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json(page([{
    id: 'u1', username: 'alice', display_name: 'Alice', created_at: 1, password_hash: 'private',
    memberships: [{ organization_id: 'self-service', organization_name: '客户工作区', role: 'customer', token: 'private' },
      { organization_id: 'org2', organization_name: '店铺2', role: 'support', password: 'private' }]
  }])))
  const result = await fetchAdminUsers(settings, admin)
  assert.equal(result.items[0].username, 'alice')
  assert.equal(result.items[0].memberships.length, 2)
  assert.deepEqual(result.items[0].memberships[0], { organization_id: 'self-service', organization_name: '客户工作区', role: 'customer' })
  assert.equal(JSON.stringify(result).includes('private'), false)
})

test('malformed membership data is rejected rather than rendered as profile fields', async context => {
  for (const memberships of ['admin', [{ organization_id: { unsafe: true } }]]) {
    context.mock.method(globalThis, 'fetch', async () => Response.json(page([{ id: 'u1', memberships }])))
    await assert.rejects(fetchAdminUsers(settings, admin), e => e.status === 502)
  }
})

test('audits retain useful status details and exclude secret and arbitrary nested payloads', async context => {
  context.mock.method(globalThis, 'fetch', async url => {
    assert.equal(new URL(url, 'https://example.invalid').pathname, '/api/python/admin/activity')
    return Response.json(page([{ id: 'e1', event_type: 'task_confirmed', actor_id: 'a1', actor_display_name: '管理员', details: { status: 'completed', revision: 2, payload_hash: 'secret', idempotency_key: 'secret', password: 'secret', token: 'secret', payload: { password: 'secret' } } }]))
  })
  const result = await fetchAdminActivity(settings, admin)
  assert.deepEqual(result.items[0].details, { status: 'completed', revision: 2 })
  assert.equal(JSON.stringify(result).includes('secret'), false)
})

test('conversation pagination preserves totals and only the documented index fields', async context => {
  context.mock.method(globalThis, 'fetch', async url => {
    assert.equal(url, '/api/python/admin/conversations?limit=20&offset=20')
    return Response.json(page([{ conversation_id: 'c1', owner_id: 'u1', owner_display_name: '用户', created_at: 10, request_count: 3, token: 'secret' }], { total: 21, offset: 20 }))
  })
  const result = await fetchAdminConversations(settings, admin, { offset: 20 })
  assert.equal(result.total, 21)
  assert.equal(result.offset, 20)
  assert.equal(result.items[0].request_count, 3)
  assert.equal(Object.hasOwn(result.items[0], 'token'), false)
})

test('task and ticket pages use admin routes and retain real ownership and expiry fields without execution secrets', async context => {
  const routes = []
  context.mock.method(globalThis, 'fetch', async url => {
    const parsed = new URL(url, 'https://example.invalid')
    routes.push(parsed.pathname)
    assert.equal(parsed.searchParams.get('limit'), '20')
    assert.equal(parsed.searchParams.get('offset'), '60')
    const items = parsed.pathname.endsWith('/tasks')
      ? [{ id: 't1', created_by: 'u1', expires_at: 123, status: 'completed', ticket_id: 'k1', payload_hash: 'secret', idempotency_key: 'secret', payload: { title: '故障', description: '排查', metadata: { password: 'secret' }, token: 'secret' } }]
      : [{ id: 'k1', created_by: 'u1', title: '故障', status: 'open', password_hash: 'secret' }]
    return Response.json(page(items, { total: 61, offset: 60 }))
  })
  const tasks = await fetchAdminTasks(settings, admin, { offset: 60 })
  const tickets = await fetchAdminTickets(settings, admin, { offset: 60 })
  assert.deepEqual(routes, ['/api/python/admin/tasks', '/api/python/admin/tickets'])
  assert.equal(tasks.total, 61)
  assert.equal(tasks.offset, 60)
  assert.equal(tasks.items[0].payload.title, '故障')
  assert.equal(tasks.items[0].status, 'completed')
  assert.equal(tasks.items[0].created_by, 'u1')
  assert.equal(tasks.items[0].expires_at, 123)
  assert.equal(tasks.items[0].ticket_id, 'k1')
  assert.equal(tickets.items[0].status, 'open')
  assert.equal(tickets.items[0].created_by, 'u1')
  assert.equal(JSON.stringify([tasks, tickets]).includes('secret'), false)
})

test('network failures are structured and AbortError remains distinguishable', async context => {
  context.mock.method(globalThis, 'fetch', async () => { throw new TypeError('failed to fetch') })
  await assert.rejects(fetchAdminOverview(settings, admin), error => error.status === 0 && error.code === 'network_error')
  context.mock.method(globalThis, 'fetch', async () => { throw new DOMException('cancelled', 'AbortError') })
  await assert.rejects(fetchAdminOverview(settings, admin), error => error.name === 'AbortError')
})
