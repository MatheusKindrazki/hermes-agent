import { expect, it, vi } from 'vitest'
import { createExecutorReader, createExecutorSender, executorReference } from './parallel-executor'
const base = 'https://mac-mini-de-matheus.tail0673c.ts.net'
it('refuses arbitrary hosts, credentials, ports, paths and invalid IDs before fetching', async () => {
  const fetcher = vi.fn()
  const read = createExecutorReader(fetcher)
  for (const url of [
    'https://evil.test/acompanhar/personal/kd-abcdef',
    base + ':8525/api/panel/resolve',
    base + ':8520/conversations/invalid',
    base + ':8526/acompanhar/personal/kd-abcdef',
    'https://x@mac-mini-de-matheus.tail0673c.ts.net:8525/acompanhar/personal/kd-abcdef'
  ])
    await expect(read(url)).rejects.toThrow()
  expect(fetcher).not.toHaveBeenCalled()
})
it('filters both task ID and front and coalesces snapshots', async () => {
  const fetcher = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          now: '2026-10-29T18:00:00Z',
          jobs: [
            { id: 'kd-abcdef', front: 'applause', title: 'Other front', state: 'queued' },
            { id: 'kd-abcdef', front: 'personal', title: 'This task', state: 'eng_running' }
          ]
        }),
        { headers: { 'content-type': 'application/json', 'last-modified': 'Thu, 29 Oct 2026 18:00:00 GMT' } }
      )
  )
  const read = createExecutorReader(fetcher)
  const result = await read(base + ':8525/acompanhar/personal/kd-abcdef')
  expect(result.task.title).toBe('This task')
  expect(JSON.stringify(result)).not.toContain('Other front')
  await read(base + ':8525/acompanhar/personal/kd-abcdef')
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledWith(
    base + ':8527/api/state',
    expect.objectContaining({ method: 'GET', redirect: 'error', credentials: 'omit' })
  )
})
it('validates conversation identity and rejects HTTP failure', async () => {
  const id = '412a934d-ca47-4fc8-a100-713ab88c5882'
  const url = base + ':8520/conversations/' + id
  expect(executorReference(url).id).toBe(id)
  await expect(createExecutorReader(async () => new Response('{}', { status: 503 }))(url)).rejects.toThrow()
  await expect(
    createExecutorReader(
      async () =>
        new Response(JSON.stringify({ id: 'other', items: [] }), { headers: { 'content-type': 'application/json' } })
    )(url)
  ).rejects.toThrow()
})

const conversationUrl = base + ':8520/conversations/412a934d-ca47-4fc8-a100-713ab88c5882'
it('sends exactly the chosen conversation and text once and preserves a queued receipt', async () => {
  const fetcher = vi.fn(
    async () =>
      new Response(JSON.stringify({ ok: true, msg: 'Mensagem salva. Aguardando vaga.' }), {
        headers: { 'content-type': 'application/json' }
      })
  )
  const receipt = await createExecutorSender(fetcher)(conversationUrl, '  Corrija o rumo: primeiro validar o escopo.  ')
  expect(receipt).toEqual({ ok: true, message: 'Mensagem salva. Aguardando vaga.' })
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledWith(
    base + ':8525/api/panel/conversation/send',
    expect.objectContaining({
      method: 'POST',
      redirect: 'error',
      credentials: 'omit',
      body: JSON.stringify({
        id: '412a934d-ca47-4fc8-a100-713ab88c5882',
        text: 'Corrija o rumo: primeiro validar o escopo.'
      })
    })
  )
})
it('rejects tasks, arbitrary hosts, blank or oversized messages without any request', async () => {
  const fetcher = vi.fn()
  const send = createExecutorSender(fetcher)
  for (const [url, text] of [
    [base + ':8525/acompanhar/personal/kd-abcdef', 'message'],
    ['https://evil.test/conversations/412a934d-ca47-4fc8-a100-713ab88c5882', 'message'],
    [conversationUrl, '  '],
    [conversationUrl, 'a'.repeat(20001)]
  ])
    await expect(send(url, text)).rejects.toThrow()
  expect(fetcher).not.toHaveBeenCalled()
})
it('never retries an ambiguous send or claims success from a failed response', async () => {
  for (const fetcher of [
    vi.fn().mockRejectedValue(new Error('timeout')),
    vi.fn().mockResolvedValue(new Response('{}', { status: 500 }))
  ]) {
    const receipt = await createExecutorSender(fetcher)(conversationUrl, 'Ajuste')
    expect(receipt.ok).toBe(false)
    expect(fetcher).toHaveBeenCalledTimes(1)
  }
})
