import test from 'node:test'
import assert from 'node:assert/strict'
import { readChatStream } from '../src/lib/chat-stream.js'

const frame = (data, separator = '\n') => `event: ${data.type}${separator}data: ${JSON.stringify(data)}${separator}${separator}`

function response(bytes) {
  return new Response(new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]))
      controller.close()
    }
  }))
}

test('handles Chinese characters and CRLF frame boundaries split between bytes', async () => {
  const events = []
  const stream = ': heartbeat\r\n\r\n' + frame({ type: 'text_delta', text: '退款审核' }, '\r\n')
    + frame({ type: 'done', response: { response: '退款审核' } }, '\r\n')
  const result = await readChatStream(response(new TextEncoder().encode(stream)), (event) => events.push(event))
  assert.equal(events[0].text, '退款审核')
  assert.equal(result.response, '退款审核')
})

test('delivers text before the final response is available', async () => {
  let controller
  const events = []
  const stream = new ReadableStream({ start(value) { controller = value } })
  const completion = readChatStream(new Response(stream), (event) => {
    events.push(event)
    if (event.type === 'text_delta') {
      controller.enqueue(new TextEncoder().encode(frame({ type: 'done', response: { response: 'hello' } })))
      controller.close()
    }
  })
  controller.enqueue(new TextEncoder().encode(frame({ type: 'text_delta', text: 'hello' })))
  assert.equal((await completion).response, 'hello')
  assert.deepEqual(events.map((event) => event.type), ['text_delta', 'done'])
})

test('reports disconnected streams rather than treating partial text as complete', async () => {
  const stream = frame({ type: 'text_delta', text: 'partial' })
  await assert.rejects(readChatStream(response(new TextEncoder().encode(stream))), /提前结束/)
})

test('propagates backend error events', async () => {
  const stream = frame({ type: 'error', message: '工具服务不可用' })
  await assert.rejects(readChatStream(response(new TextEncoder().encode(stream))), /工具服务不可用/)
})
