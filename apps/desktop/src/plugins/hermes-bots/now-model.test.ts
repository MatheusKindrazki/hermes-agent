import { describe, expect, it } from 'vitest'

import { nowGroups } from './now-model'
import type { RosterRow } from './types'

const now = 1800000000000

const bot = (name: string, secondsAgo = 60): RosterRow => ({
  name,
  canonical_session: { id: `chat-${name}`, last_active: now / 1000 - secondsAgo, preview: 'An update' }
})

describe('Now triage uses canonical evidence', () => {
  it('prioritizes requests over running and recent entries, without duplicates', () => {
    const a = bot('waiting'),
      b = bot('working'),
      c = bot('recent'),
      d = bot('old', 90000)

    const result = nowGroups(
      [a, b, c, d],
      { 'chat-waiting': 'think', 'chat-working': 'work' },
      { 'chat-waiting': 'needs-input' },
      () => false,
      now
    )

    expect(result.attention).toEqual([a])
    expect(result.running).toEqual([b])
    expect(result.recent).toEqual([c])
  })
  it('never treats an unrelated session, recency or an offline snapshot as a live job', () => {
    const a = bot('ready'),
      b = { ...bot('offline'), sourceReachable: false },
      c = { ...bot('retained'), retained: true }

    const result = nowGroups(
      [a, b, c],
      { unrelated: 'work', 'chat-offline': 'work', 'chat-retained': 'work' },
      {},
      () => false,
      now
    )

    expect(result.running).toEqual([])
    expect(result.recent).toContain(a)
  })
  it('includes fresh background workers and attention failures', () => {
    const worker = { ...bot('worker'), worker_session: { last_active: now / 1000 - 30 } }
    const failed = bot('failed')
    const result = nowGroups([worker, failed], {}, {}, item => item.name === 'failed', now)
    expect(result.running).toEqual([worker])
    expect(result.attention).toEqual([failed])
  })
})
