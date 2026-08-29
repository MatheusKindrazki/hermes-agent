import { useStore } from '@nanostores/react'

import { Tip } from '@/components/ui/tooltip'
import { HelpCircle } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $connectionsRegistry } from '@/store/connection-registry-state'

import { type ActiveContext, activeContextLabels } from './active-context'
import { ConnectionGlyph } from './sidebar/connection-glyph'

/**
 * States the destination of the next message: which profile, on which machine.
 *
 * The text is RENDERED, not just announced. Identity that lives only in a
 * tooltip or an aria-label is identity the user has to go hunting for, and the
 * complaint being fixed is that they cannot tell at a glance where the next
 * message lands. A hover is not a glance.
 *
 * Profile alone would not be enough even if it were visible: two sources
 * commonly both expose a `default` profile, so a chat that does not name its
 * machine cannot be told apart from the same-named chat on another one. That
 * is also what disambiguates N tabs all legitimately titled "Bot Chat" without
 * touching the title — which is simultaneously the registry key, the sweep key
 * and the server-side canonical-chat resolver input, so it must not be edited.
 *
 * The conversation itself is the header title beside this chip; together they
 * state profile, machine and chat.
 */
export function ActiveContextChip({ className, context }: { className?: string; context: ActiveContext }) {
  const registry = useStore($connectionsRegistry)

  const connection = context.connectionId
    ? registry?.connections.find(candidate => candidate.id === context.connectionId)
    : undefined

  const { detail, text } = activeContextLabels(context, connection?.label ?? null)
  const conversation = context.storedSessionId?.trim() || (context.source === 'draft' ? 'draft' : 'unknown')

  return (
    <Tip label={detail}>
      <span
        aria-label={detail}
        className={cn('inline-flex min-w-0 items-center gap-1 text-xs text-(--ui-text-tertiary)', className)}
        data-active-context-connection={context.connectionId?.trim() || 'unknown'}
        data-active-context-conversation={conversation}
        data-active-context-machine={
          context.machine?.trim() || connection?.installId?.trim() || connection?.label || 'unknown'
        }
        data-active-context-profile={context.profile?.trim() || 'unknown'}
        data-active-context-source={context.source}
        data-active-context-tenant={context.tenant?.trim() || 'unknown'}
        data-slot="active-context-chip"
      >
        {connection ? (
          <ConnectionGlyph connection={connection} />
        ) : (
          // An unnamed destination must still be visible. Rendering nothing
          // here would read as "nothing to say about ownership" rather than
          // "this is the one thing I cannot tell you".
          <span
            aria-hidden="true"
            className="grid size-3.5 shrink-0 place-items-center text-(--ui-text-quaternary)"
            data-slot="active-context-unknown"
          >
            <HelpCircle className="size-3" />
          </span>
        )}
        <span className="truncate" data-slot="active-context-text">
          {text}
        </span>
      </span>
    </Tip>
  )
}
