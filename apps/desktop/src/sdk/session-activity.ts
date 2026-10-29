/** Stored-session activity, independent of tile focus and compression aliases. */
import { computed } from 'nanostores'

import { stableArray, stableRecord } from '@/lib/stable-array'
import { $sessions, lineageAliases } from '@/store/session'
import { $sessionDotStateById } from '@/store/session-dot-state'
import { $executingToolsByRuntime } from '@/store/session-executing-tools'
import { $sessionStates } from '@/store/session-states'

export type SessionWorkMood = 'idle' | 'think' | 'work' | 'waiting'
let previous: Readonly<Record<string, SessionWorkMood>> = {}

export const $sessionWorkById = computed(
  [$sessionDotStateById, $sessionStates, $sessions, $executingToolsByRuntime],
  (statuses, states, sessions, executingTools) => {
    const next: Record<string, SessionWorkMood> = {}

    for (const [id, status] of Object.entries(statuses)) {
      next[id] =
        status === 'needs-input'
          ? 'waiting'
          : status === 'background'
            ? 'work'
            : ['working', 'stalled'].includes(status)
              ? 'think'
              : 'idle'
    }

    for (const [runtimeId, state] of Object.entries(states)) {
      if (!state.busy || state.needsInput || state.interrupted) {
        continue
      }

      const message = state.streamId
        ? state.messages.findLast(item => item.id === state.streamId)
        : state.messages.at(-1)

      const executing =
        Object.keys(executingTools[runtimeId] || {}).length > 0 ||
        message?.parts.some(
          part => part.type === 'tool-call' && part.result === undefined && !part.interrupted && !part.completedAt
        )

      if (!executing) {
        continue
      }

      for (const id of lineageAliases(state.storedSessionId ?? runtimeId, sessions)) {
        if (next[id] === 'think') {
          next[id] = 'work'
        }
      }
    }

    return (previous = stableRecord(previous, next))
  }
)

/** Tool names only: no arguments, command text, results or credentials. */
let previousTools: Readonly<Record<string, readonly string[]>> = {}
export const $sessionToolsById = computed(
  [$sessionStates, $sessions, $executingToolsByRuntime],
  (states, sessions, executingTools) => {
    const next: Record<string, readonly string[]> = {}

    for (const [runtimeId, state] of Object.entries(states)) {
      if (!state.busy || state.needsInput || state.interrupted) {
        continue
      }

      const message = state.streamId
        ? state.messages.findLast(item => item.id === state.streamId)
        : state.messages.at(-1)

      const observedNames = Object.values(executingTools[runtimeId] || {})

      const names = observedNames.length
        ? observedNames
        : (message?.parts || []).flatMap(part =>
            part.type === 'tool-call' && part.result === undefined && !part.interrupted && !part.completedAt
              ? [part.toolName]
              : []
          )

      for (const id of lineageAliases(state.storedSessionId ?? runtimeId, sessions)) {
        next[id] = stableArray(previousTools[id] || [], names)
      }
    }

    return (previousTools = stableRecord(previousTools, next))
  }
)
