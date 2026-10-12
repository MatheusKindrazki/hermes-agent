import { useStore } from '@nanostores/react'
import type { CSSProperties } from 'react'
import { useState } from 'react'

import { useStoreSelector } from '@/lib/use-session-slice'
import { avatarColor, botAppearance, BotFace, personaFor } from '@/plugins/hermes-bots/avatar'
import { $botMeta, $lastRoster } from '@/plugins/hermes-bots/data'
import { displayName } from '@/plugins/hermes-bots/labels'
import { paintedPersonaColor } from '@/plugins/hermes-bots/painted-avatar'
import { botRosterMeta } from '@/plugins/hermes-bots/routing'
import { botWorkingMood } from '@/plugins/hermes-bots/row-helpers'
import type { RosterRow } from '@/plugins/hermes-bots/types'
import { $sessionWorkById } from '@/sdk/session-activity'
import { $sessionDotStateById } from '@/store/session-dot-state'

/** Resolve the exact canonical conversation, never the profile's latest activity. */
export function conversationBot(roster: RosterRow[], storedId: string | null): RosterRow | null {
  if (!storedId) {
    return null
  }

  const matches = roster.filter(
    bot => bot.canonical_session?.id === storedId || bot.canonical_session?.resolved_id === storedId
  )

  return matches.length === 1 ? matches[0] : null
}

export function useChatPersona(storedId: string | null) {
  const bot = useStoreSelector($lastRoster, roster => conversationBot(roster, storedId))
  const metas = useStore($botMeta)

  if (!bot) {
    return null
  }

  const meta = botRosterMeta(bot, metas)
  const appearance = botAppearance(bot.name, meta)
  const color = paintedPersonaColor(personaFor(bot.name).role) || avatarColor(appearance.color, bot.name)

  return {
    bot,
    appearance,
    title: displayName(bot, meta),
    color,
    style: { '--chat-persona-accent': color } as CSSProperties
  }
}

export function ChatPersonaHeader({ persona }: { persona: NonNullable<ReturnType<typeof useChatPersona>> }) {
  const { bot, appearance, title, color } = persona
  const id = bot.canonical_session?.resolved_id ?? bot.canonical_session?.id ?? ''

  const mood = useStoreSelector($sessionWorkById, values =>
    botWorkingMood(bot, null, false, 'local', Date.now(), undefined, values)
  )

  const status = useStoreSelector($sessionDotStateById, values => values[id] || 'idle')
  const [hovered, setHovered] = useState(false)

  const label =
    status === 'needs-input'
      ? 'Aguardando sua resposta'
      : mood === 'work'
        ? 'Trabalhando'
        : mood === 'think'
          ? 'Pensando'
          : 'Pronto para conversar'

  return (
    <header className="chat-persona-header" data-status={status}>
      <div
        className="chat-persona-header__identity"
        onPointerEnter={() => setHovered(true)}
        onPointerLeave={() => setHovered(false)}
      >
        <div className="chat-persona-header__avatar">
          <BotFace
            color={color}
            hovered={hovered}
            image={appearance.image}
            mood={mood}
            name={bot.name}
            shape={appearance.shape}
            size={48}
          />
        </div>
        <div className="min-w-0">
          <h1 className="truncate text-[0.9375rem] font-semibold text-(--ui-text-primary)">{title}</h1>
          <p className="mt-1 text-[0.6875rem] text-(--ui-text-tertiary)">{label}</p>
        </div>
      </div>
      <span className="chat-persona-header__source" title={bot.connectionLabel || ''}>
        {bot.connectionLabel || 'Bot Chat'}
      </span>
    </header>
  )
}
