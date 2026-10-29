/** Read-only access to the user's tailnet executor. Never fetch a chat-provided origin. */
const HOST = 'mac-mini-de-matheus.tail0673c.ts.net'
const ORIGIN = `https://${HOST}:8525`
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i
export function executorReference(value: unknown) {
  if (typeof value !== 'string') throw new Error('Invalid reference')
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.hostname !== HOST || url.username || url.password)
    throw new Error('Unknown executor')
  const task = /^\/acompanhar\/(personal|applause|ceia)\/(kd-[0-9a-f]{6,})\/?$/.exec(url.pathname)
  if (task && url.port === '8525') return { kind: 'task' as const, front: task[1], id: task[2] }
  const conv = /^\/conversations\/([^/]+)\/?$/.exec(url.pathname)
  if (conv && UUID.test(conv[1]) && url.port === '8520') return { kind: 'conversation' as const, id: conv[1] }
  throw new Error('Unsupported executor reference')
}
export function createExecutorReader(fetcher: typeof fetch = fetch) {
  // Coalesce the shared task snapshot without retaining unrelated data in the renderer.
  let snapshot: { at: number; value: Promise<{ data: any; sourceAt: string | null }> } | undefined
  async function get(path: string, origin = ORIGIN) {
    const response = await fetcher(origin + path, {
      method: 'GET',
      redirect: 'error',
      credentials: 'omit',
      signal: AbortSignal.timeout(12000)
    })
    if (!response.ok) throw new Error('Executor unavailable')
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Invalid response')
    const reader = response.body?.getReader()
    if (!reader) throw new Error('Empty response')
    let text = '',
      size = 0
    const decoder = new TextDecoder()
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > 4_000_000) throw new Error('Response too large')
        text += decoder.decode(value, { stream: true })
      }
      text += decoder.decode()
      return { data: JSON.parse(text), sourceAt: response.headers.get('last-modified') }
    } finally {
      await reader.cancel().catch(() => {})
    }
  }
  return async (url: unknown) => {
    const ref = executorReference(url)
    if (ref.kind === 'conversation') {
      const { data } = await get('/api/panel/conversation?id=' + encodeURIComponent(ref.id))
      if (data.id !== ref.id || !Array.isArray(data.items)) throw new Error('Mismatched conversation')
      return { ...data, kind: 'conversation', fetchedAt: new Date().toISOString() }
    }
    if (!snapshot || Date.now() - snapshot.at > 4000) {
      const value = get('/api/state', `https://${HOST}:8527`)
      snapshot = { at: Date.now(), value }
      value.catch(() => {
        if (snapshot?.value === value) snapshot = undefined
      })
    }
    const { data } = await snapshot.value
    if (!Array.isArray(data.jobs)) throw new Error('Invalid task snapshot')
    const job = data.jobs.find((x: any) => x.id === ref.id && x.front === ref.front)
    const states: Record<string, string> = {
      queued: 'Na fila',
      eng_running: 'Engenharia em andamento',
      qa_running: 'QA em andamento',
      pr_publish: 'Publicando revisão',
      pr_watch: 'Acompanhando revisão',
      ready_for_review: 'Pronta para revisão',
      approved: 'Aprovada',
      blocked: 'Bloqueada',
      waiting_host: 'Aguardando máquina',
      waiting_busy: 'Aguardando agente',
      cancelled: 'Cancelada',
      failed: 'Falhou'
    }
    if (job) {
      const convs = [
        ['eng_conv', 'Engenharia'],
        ['qa_conv', 'QA'],
        ['report_conv', 'Coordenação']
      ].flatMap(([key, label]) =>
        UUID.test(job[key] || '')
          ? [{ id: job[key], label, url: `https://${HOST}:8520/conversations/${job[key]}` }]
          : []
      )
      const task = {
        title: job.title,
        state: states[job.state] || job.state,
        reason: job.reason,
        convs,
        timeline: (job.events || [])
          .slice(0, 14)
          .map((event: any) => ({
            at: event.at,
            text: event.data?.message || event.data?.summary || event.kind.replaceAll('_', ' ')
          }))
      }
      return {
        kind: 'task',
        task,
        live: true,
        sourceAt: data.now,
        publishedAt: data.now,
        fetchedAt: new Date().toISOString()
      }
    }
    // The live endpoint only retains its latest 40 jobs. Older references remain explicitly snapshots.
    const archived = await get('/painel/data.json')
    const task = archived.data.deliveries?.find((x: any) => x.id === ref.id && x.front === ref.front)
    return {
      kind: 'task',
      task: task ?? null,
      live: false,
      sourceAt: archived.sourceAt,
      publishedAt: archived.data.at,
      fetchedAt: new Date().toISOString()
    }
  }
}
