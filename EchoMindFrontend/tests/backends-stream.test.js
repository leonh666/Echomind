import test from 'node:test'
import assert from 'node:assert/strict'
import { requestChatStream } from '../src/lib/backends.js'

const settings = { userId: 'u-test', endpoints: { python: '/api/python', java: '/api/java' } }

test('uses real stream events and normalizes the final chat response', async (context) => {
  const events = []
  const body = 'data: {"type":"text_delta","text":"你好"}\n\n'
    + 'data: {"type":"done","response":{"response":"你好","conv_id":"c1","agent_type":"general"}}\n\n'
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/python/chat/stream')
    assert.equal(options.headers.Accept, 'text/event-stream')
    return new Response(body)
  })
  const result = await requestChatStream('python', settings, '你好', (event) => events.push(event))
  assert.equal(events[0].text, '你好')
  assert.equal(result.conversationId, 'c1')
  assert.equal(result.response, '你好')
})

test('old servers without a stream endpoint fall back once with cancellation intact', async (context) => {
  const calls = []
  const events = []
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    assert.equal(options.signal, controller.signal)
    return calls.length === 1 ? new Response('', { status: 404 })
      : Response.json({ response: '旧接口回复', conv_id: 'c2' })
  })
  const result = await requestChatStream('python', settings, '你好', (event) => events.push(event), controller.signal)
  assert.deepEqual(calls, ['/api/python/chat/stream', '/api/python/chat'])
  assert.equal(events[0].type, 'stage')
  assert.equal(result.response, '旧接口回复')
})

test('server failures do not silently repeat a chat request', async (context) => {
  let calls = 0
  context.mock.method(globalThis, 'fetch', async () => {
    calls += 1
    return new Response('服务未就绪', { status: 503 })
  })
  await assert.rejects(requestChatStream('python', settings, '你好'), /503/)
  assert.equal(calls, 1)
})
