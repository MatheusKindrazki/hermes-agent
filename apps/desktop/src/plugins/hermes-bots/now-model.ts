import { botSourceStatus } from './data'
import { botCanonicalSessionId, workerActiveAt } from './row-helpers'
import type { RosterRow } from './types'

export interface NowGroups {
  attention: RosterRow[]
  running: RosterRow[]
  recent: RosterRow[]
}

/** Mutually exclusive buckets. Recency alone never claims a running job or completion. */
export function nowGroups(
  bots: RosterRow[],
  moods: Readonly<Record<string, string>>,
  statuses: Readonly<Record<string, string>>,
  attention: (bot: RosterRow) => boolean,
  now = Date.now()
): NowGroups {
  const result: NowGroups = { attention: [], running: [], recent: [] }

  for (const bot of bots) {
    const id = botCanonicalSessionId(bot)
    const status = id ? statuses[id] : undefined
    const mood = id ? moods[id] : undefined

    if (status === 'needs-input' || attention(bot)) {
      result.attention.push(bot)
    } else if (
      !bot.retained &&
      botSourceStatus(bot).available &&
      (mood === 'think' || mood === 'work' || workerActiveAt(bot, now))
    ) {
      result.running.push(bot)
    } else {
      const last = bot.canonical_session?.last_active || 0

      if (
        status === 'unread' ||
        (last > 0 && now / 1000 - last >= 0 && now / 1000 - last < 86400 && bot.canonical_session?.preview)
      ) {
        result.recent.push(bot)
      }
    }
  }

  result.recent.sort((a, b) => (b.canonical_session?.last_active || 0) - (a.canonical_session?.last_active || 0))

  return result
}
