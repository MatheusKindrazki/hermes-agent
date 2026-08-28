import { useStore } from '@nanostores/react'

import { Tip } from '@/components/ui/tooltip'
import { HelpCircle } from '@/lib/icons'
import { $connectionsRegistry } from '@/store/connection-registry-state'

import type { ActiveContext } from './active-context'

import { ConnectionGlyph } from './sidebar/connection-glyph'

/**
 * States the destination of the next message: profile, connection, session.
 *
 * The profile glyph beside it already names the persona; this adds the missing
 * half. Profile alone is not an identity — two sources commonly both expose a
 * `default` profile — so a chat that does not say which machine it belongs to
 * cannot be told apart from the same-named chat on another one. That is also
 * what disambiguates N tabs all legitimately titled "Bot Chat" without
 * touching the title, which is simultaneously the registry key, the sweep key
 * and the server-side canonical-chat resolver input.
 *
 * Renders "unknown" honestly rather than borrowing the ambient connection: a
 * confident wrong machine name is worse than an admitted gap.
 */
export function ActiveContextChip({ className, context }: { className?: string; context: ActiveContext }) {
  const registry = useStore($connectionsRegistry)
  const connection = context.connectionId
    ? registry?.connections.find(candidate => candidate.id === context.connectionId)
    : undefined

  const where = connection?.label ?? (context.connectionId ? context.connectionId : 'Unknown device')
  const what =
    context.source === 'draft'
      ? 'New chat'
      : context.source === 'unknown'
        ? 'Session owner unknown'
        : 'This chat'
  const label = `${what} · ${context.profile ?? 'Unknown profile'} on ${where}`

  return (
    <Tip label={label}>
      <span
        aria-label={label}
        className={className}
        data-active-context-source={context.source}
        data-slot="active-context-chip"
        role="img"
      >
        {connection ? (
          <ConnectionGlyph connection={connection} />
        ) : (
          // An unnamed destination must still be VISIBLE. Rendering nothing
          // here would reproduce the reported symptom in miniature: no glyph
          // reads as "nothing to say about ownership" rather than "this is the
          // one thing I cannot tell you".
          <span
            aria-hidden="true"
            className="grid size-3.5 shrink-0 place-items-center text-(--ui-text-quaternary)"
            data-slot="active-context-unknown"
          >
            <HelpCircle className="size-3" />
          </span>
        )}
      </span>
    </Tip>
  )
}
