// Parse SSE frames across arbitrary byte boundaries, including UTF-8 characters.
export async function readChatStream(response, onEvent) {
  if (!response.body) throw new Error('浏览器无法读取实时回复。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result

  function dispatch(frame) {
    const data = frame.split(/\r?\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart()).join('\n')
    if (!data) return
    const event = JSON.parse(data)
    if (event.type === 'error') throw new Error(event.message || '生成回复失败。')
    onEvent?.(event)
    if (event.type === 'done') result = event.response
  }

  try {
    while (result === undefined) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      let boundary
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        dispatch(buffer.slice(0, boundary.index))
        buffer = buffer.slice(boundary.index + boundary[0].length)
      }
      if (done) {
        if (buffer.trim()) dispatch(buffer)
        break
      }
    }
    if (result === undefined) throw new Error('回复连接提前结束，请重试。')
    return result
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
