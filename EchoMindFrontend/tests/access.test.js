import test from 'node:test'
import assert from 'node:assert/strict'
import { identityKey, workspaceAccess } from '../src/lib/access.js'

const member = role => ({ user_id: 'u1', organization_id: 'org1', role })
test('customers see their workspace without any backend entry', () => {
  assert.deepEqual(workspaceAccess(member('customer')).views, ['chat', 'profile', 'tickets', 'answers'])
  assert.equal(workspaceAccess(member('customer')).admin, false)
  assert.deepEqual(workspaceAccess(null).views, ['chat'])
  assert.equal(workspaceAccess({ role: 'admin' }).admin, false)
})
test('staff retain only their assigned capabilities', () => {
  assert.deepEqual(workspaceAccess(member('support')).views, ['chat', 'profile', 'tickets', 'answers'])
  assert.deepEqual(workspaceAccess(member('kb_admin')).views, ['chat', 'profile', 'tickets', 'answers', 'knowledge'])
  assert.deepEqual(workspaceAccess(member('admin')).views, ['chat', 'profile', 'tickets', 'answers', 'knowledge', 'admin', 'evaluation', 'diagnostics'])
})
test('identity invalidation includes role and organization, accepting login and refresh shapes', () => {
  assert.equal(identityKey(member('customer')), identityKey({ user: { id: 'u1' }, organization_id: 'org1', role: 'customer' }))
  assert.notEqual(identityKey(member('admin')), identityKey(member('customer')))
  assert.notEqual(identityKey(member('admin')), identityKey({ ...member('admin'), organization_id: 'org2' }))
  assert.equal(identityKey(null), '')
})
