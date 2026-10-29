import { useValue } from '@hermes/plugin-sdk'
import type { ReactNode } from 'react'

import { useActivityStatus } from './activity-preview'
import { $botAttention, botRosterKey, botSelectionKey } from './data'
import { nowGroups } from './now-model'
import { useSessionWork } from './row-helpers'
import type { GroupMember, RosterRow } from './types'

interface NowRoom {
  name: string
  members: GroupMember[]
  waiting: boolean
  running: boolean
  updated: number
}

/** A calm triage view: requests first, live work second, recent updates last. */
export function NowView({
  bots,
  rooms,
  renderBot,
  renderRoom,
  onShowAll,
  loading,
  stale
}: {
  bots: RosterRow[]
  rooms: NowRoom[]
  renderBot: (bot: RosterRow, prefix?: string) => ReactNode
  renderRoom: (room: NowRoom) => ReactNode
  onShowAll: () => void
  loading: boolean
  stale?: boolean
}) {
  const moods = useSessionWork()
  const statuses = useActivityStatus()
  const alerts = useValue($botAttention)

  const buckets = nowGroups(bots, moods, statuses, bot =>
    Boolean(alerts[botRosterKey(bot)] || alerts[botSelectionKey(bot)])
  )

  const sections = [
    {
      key: 'attention' as const,
      label: 'Precisa de você',
      empty: 'Nenhum pedido pendente.',
      rooms: rooms.filter(room => room.waiting),
      tone: 'text-amber-600 dark:text-amber-300'
    },
    {
      key: 'running' as const,
      label: 'Em andamento',
      empty: 'Sem atividade registrada agora.',
      rooms: rooms.filter(room => !room.waiting && room.running),
      tone: 'text-(--ui-text-secondary)'
    },
    {
      key: 'recent' as const,
      label: 'Atualizações recentes',
      empty: 'Novas atualizações aparecem aqui.',
      rooms: rooms.filter(room => !room.waiting && !room.running && room.updated > Date.now() - 86400000),
      tone: 'text-(--ui-text-secondary)'
    }
  ]

  return (
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-1.5 pb-3" data-slot="bots-now">
      {(loading || stale) && (
        <p className="px-2.5 pb-3 text-[0.6875rem] leading-relaxed text-(--ui-text-tertiary)">
          {stale ? 'As atualizações podem estar desatualizadas.' : 'Atualizando atividades…'}
        </p>
      )}
      {sections.map(section => {
        const rows = buckets[section.key]
        const count = rows.length + section.rooms.length

        if (!count && section.key !== 'recent') {
          return null
        }

        return (
          <section aria-label={section.label} className="mb-5" key={section.key}>
            <div className="flex items-center justify-between px-2.5 pb-2">
              <h3 className={`text-[0.6875rem] font-medium ${count ? section.tone : 'text-(--ui-text-tertiary)'}`}>
                {section.label}
              </h3>
              {count > 0 && (
                <span className="rounded-full bg-(--chrome-action-hover) px-1.5 text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">
                  {count}
                </span>
              )}
            </div>
            {!count ? (
              <p className="px-2.5 py-0.5 text-[0.6875rem] leading-relaxed text-(--ui-text-quaternary)">
                {section.empty}
              </p>
            ) : (
              <div className="grid gap-0.5">
                {rows
                  .slice(0, section.key === 'recent' ? 5 : rows.length)
                  .map(bot => renderBot(bot, `now:${section.key}:`))}
                {section.rooms.map(renderRoom)}
              </div>
            )}
            {section.key === 'recent' && rows.length > 5 && (
              <button
                className="mx-2 mt-1 rounded px-1 py-1 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary) focus-visible:outline focus-visible:outline-2"
                onClick={onShowAll}
                type="button"
              >
                Ver todas as atualizações em Todos
              </button>
            )}
          </section>
        )
      })}
      <button
        className="mx-2 rounded-md px-2 py-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) focus-visible:outline focus-visible:outline-2"
        onClick={onShowAll}
        type="button"
      >
        Explorar todo o time →
      </button>
    </div>
  )
}
