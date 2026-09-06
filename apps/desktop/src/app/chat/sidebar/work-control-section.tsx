import { useCallback, useEffect, useState } from 'react'

import {
  getWorkControlProjection,
  type WorkControlProjection
} from '@/api/work-control'

const REFRESH_MS = 30_000

export function WorkControlSection() {
  const [projection, setProjection] = useState<WorkControlProjection | null>(null)
  const [unavailable, setUnavailable] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const next = await getWorkControlProjection()
      setProjection(next)
      setUnavailable(false)
    } catch {
      // Preserve a previously rendered snapshot. The backend is the freshness
      // authority; the renderer merely adds an unavailable signal when the
      // local request itself fails.
      setUnavailable(true)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const interval = window.setInterval(() => void refresh(), REFRESH_MS)

    return () => window.clearInterval(interval)
  }, [refresh])

  const stale = unavailable || projection?.stale === true

  return (
    <section
      aria-label="Work Control"
      className="mx-2 mb-2 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-control-background) px-2 py-1.5 text-xs"
    >
      <div className="flex items-center justify-between gap-2 text-(--ui-text-secondary)">
        <span className="font-medium">Work Control</span>
        {stale && (
          <span className="rounded-sm bg-(--ui-control-hover-background) px-1.5 py-0.5 text-[0.625rem] uppercase tracking-wide text-(--ui-text-tertiary)">
            {projection ? 'Stale' : 'Unavailable'}
          </span>
        )}
      </div>

      {!projection ? (
        <p className="mt-1 text-(--ui-text-tertiary)">Work Control unavailable</p>
      ) : projection.records.length === 0 ? (
        <p className="mt-1 text-(--ui-text-tertiary)">No active work</p>
      ) : (
        <ul aria-label="Current work" className="mt-1 space-y-1">
          {projection.records.map(record => (
            <li className="flex min-w-0 items-center gap-1.5" key={record.work_id}>
              <span className="min-w-0 flex-1 truncate text-foreground">{record.front}</span>
              {record.profile && (
                <span className="max-w-20 truncate text-(--ui-text-tertiary)">{record.profile}</span>
              )}
              <span className="shrink-0 text-(--ui-text-secondary)">{record.status}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
