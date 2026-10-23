import test from 'node:test'
import assert from 'node:assert/strict'
import { login, register } from '../src/lib/operations.js'

const settings = { endpoints: { python: '/api/python' } }

test('username login uses session cookies without requesting bearer tokens', async context => {
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/python/auth/login')
    assert.equal(options.credentials, 'include')
    assert.deepEqual(JSON.parse(options.body), { username: 'alice', password: 'local-password-1234' })
    return Response.json({ role: 'customer' })
  })
  assert.equal((await login(settings, 'alice', 'local-password-1234')).role, 'customer')
})

test('registration transmits only username and password, retaining structured errors', async context => {
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/python/auth/register')
    assert.deepEqual(JSON.parse(options.body), { username: 'alice', password: 'local-password-1234' })
    return Response.json({ detail: { code: 'user_exists', message: '用户名或邮箱已被使用' } }, { status: 409 })
  })
  await assert.rejects(register(settings, 'alice', 'local-password-1234'), e => e.status === 409 && e.code === 'user_exists')
})
