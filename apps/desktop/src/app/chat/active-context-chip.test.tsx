import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $connectionsRegistry } from '@/store/connection-registry-state'

import type { ActiveContext } from './active-context'

import { ActiveContextChip } from './active-context-chip'

const REGISTRY = {
  connections: [
    { id: 'homelab', kind: 'ssh' as const, label: 'Homelab', tokenSet: false },
    { id: 'this-mac', kind: 'local' as const, label: 'This Mac', tokenSet: false }
  ],
  primary: 'this-mac',
  secureTokenStorage: true,
  version: 1
}

const SESSION_ON_HOMELAB: ActiveContext = {
  connectionId: 'homelab',
  profile: 'research',
  source: 'session',
  storedSessionId: 'chat-a'
}

beforeEach(() => {
  $connectionsRegistry.set(REGISTRY as never)
})

afterEach(() => {
  cleanup()
  $connectionsRegistry.set(null)
})

describe('ActiveContextChip', () => {
  it('renders profile and machine as VISIBLE text, not only a tooltip', () => {
    // The regression this pins: moving the identity into the tooltip or the
    // aria-label alone. getByText only passes when the strings are painted.
    render(<ActiveContextChip context={SESSION_ON_HOMELAB} />)

    expect(screen.getByText(/research/)).toBeTruthy()
    expect(screen.getByText(/Homelab/)).toBeTruthy()
  })

  it('keeps the visible text non-empty even when nothing can be derived', () => {
    render(
      <ActiveContextChip
        context={{ connectionId: null, profile: null, source: 'unknown', storedSessionId: 'chat-a' }}
      />
    )

    const painted = screen.getByText(/unknown profile/)

    expect(painted).toBeTruthy()
    expect(screen.getByText(/unknown device/)).toBeTruthy()
  })

  it('states where a draft would land', () => {
    render(
      <ActiveContextChip
        context={{ connectionId: 'this-mac', profile: 'default', source: 'draft', storedSessionId: null }}
      />
    )

    expect(screen.getByText(/default/)).toBeTruthy()
    expect(screen.getByText(/This Mac/)).toBeTruthy()
  })

  it('distinguishes two chats that share the canonical Bot Chat title', () => {
    // The Bot Chat title is the registry key, the sweep key and the
    // server-side canonical-chat resolver input, so it is never edited to
    // disambiguate. The chip carries the difference instead.
    const { unmount } = render(
      <ActiveContextChip
        context={{ connectionId: 'homelab', profile: 'bots', source: 'session', storedSessionId: 'bot-chat' }}
      />
    )

    expect(screen.getByText(/Homelab/)).toBeTruthy()
    unmount()

    render(
      <ActiveContextChip
        context={{ connectionId: 'this-mac', profile: 'bots', source: 'session', storedSessionId: 'bot-chat' }}
      />
    )

    expect(screen.getByText(/This Mac/)).toBeTruthy()
  })
})
