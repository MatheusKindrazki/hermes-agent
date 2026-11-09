import { computed, type ReadableAtom } from 'nanostores'

import type { ClientSessionState } from '@/app/types'
import { $selectedStoredSessionId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $focusedSessionState } from '@/store/session-states'

export const focusedTurnFlag = (
  select: (state: ClientSessionState) => boolean,
  $primary: ReadableAtom<boolean>
): ReadableAtom<boolean> =>
  computed(
    [$focusedStoredSessionId, $selectedStoredSessionId, $focusedSessionState, $primary],
    (focused, selected, state, primary) =>
      !focused || focused === selected ? primary : Boolean(state && select(state))
  )
