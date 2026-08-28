import { describe, expect, it } from 'vitest'

import { NEW_CHAT_ROUTE, routeSessionId, SETTINGS_ROUTE } from '../routes'

import {
  activeContextLabels,
  resolveActiveContext,
  sameActiveContext,
  shouldPreserveRouteAcrossGatewaySwitch
} from './active-context'

describe('resolveActiveContext', () => {
  it('names the session own owner, not whatever backend the window is showing', () => {
    const context = resolveActiveContext({
      newChatRoute: { connectionId: 'ambient-conn', profile: 'ambient' },
      owner: { connectionId: 'homelab', profile: 'research' },
      targetStoredSessionId: 'chat-a'
    })

    expect(context).toEqual({
      connectionId: 'homelab',
      profile: 'research',
      source: 'session',
      storedSessionId: 'chat-a'
    })
  })

  it('states where a draft would land instead of showing nothing', () => {
    // This is the case the header used to suppress entirely — and it is
    // exactly when the user most needs to know where Enter goes.
    const context = resolveActiveContext({
      newChatRoute: { connectionId: 'work-laptop', profile: 'default' },
      owner: undefined,
      targetStoredSessionId: null
    })

    expect(context).toEqual({
      connectionId: 'work-laptop',
      profile: 'default',
      source: 'draft',
      storedSessionId: null
    })
  })

  it('reports unknown rather than borrowing the ambient connection', () => {
    // A confident lie about which machine receives the message is worse than
    // "unknown": ids are recyclable across backends, so the ambient guess can
    // name another machine's conversation.
    const context = resolveActiveContext({
      newChatRoute: { connectionId: 'ambient-conn', profile: 'ambient' },
      owner: undefined,
      targetStoredSessionId: 'chat-from-a-backend-we-left'
    })

    expect(context).toEqual({
      connectionId: null,
      profile: null,
      source: 'unknown',
      storedSessionId: 'chat-from-a-backend-we-left'
    })
  })

  it('keeps a bare profile from passing as a full identity', () => {
    // Two sources commonly both expose `default`, so a profile name alone
    // must not imply a connection.
    const context = resolveActiveContext({
      newChatRoute: { connectionId: 'ambient-conn', profile: 'ambient' },
      owner: 'default',
      targetStoredSessionId: 'chat-b'
    })

    expect(context.connectionId).toBeNull()
    expect(context.profile).toBe('default')
    expect(context.source).toBe('session')
  })

  it('prefers the hint target profile when a route carries one', () => {
    const context = resolveActiveContext({
      newChatRoute: null,
      owner: { connectionId: 'homelab', profile: 'default', targetProfile: 'bots' },
      targetStoredSessionId: 'chat-c'
    })

    expect(context.profile).toBe('bots')
  })

  it('treats blank strings as absent so a chip never renders an empty name', () => {
    const context = resolveActiveContext({
      newChatRoute: { connectionId: '   ', profile: '  ' },
      owner: undefined,
      targetStoredSessionId: '   '
    })

    expect(context).toEqual({
      connectionId: null,
      profile: null,
      source: 'draft',
      storedSessionId: null
    })
  })

  it('routes the draft to null when no registry source can be named', () => {
    // A legacy profile-only activation yields no route; the chip must say so
    // rather than invent one.
    const context = resolveActiveContext({
      newChatRoute: null,
      owner: undefined,
      targetStoredSessionId: null
    })

    expect(context).toEqual({
      connectionId: null,
      profile: null,
      source: 'draft',
      storedSessionId: null
    })
  })
})

describe('sameActiveContext', () => {
  it('is true only when every dimension of the destination agrees', () => {
    const base = {
      connectionId: 'homelab',
      profile: 'research',
      source: 'session',
      storedSessionId: 'chat-a'
    } as const

    expect(sameActiveContext(base, { ...base })).toBe(true)
    expect(sameActiveContext(base, { ...base, connectionId: 'other' })).toBe(false)
    expect(sameActiveContext(base, { ...base, profile: 'other' })).toBe(false)
    expect(sameActiveContext(base, { ...base, storedSessionId: 'chat-b' })).toBe(false)
    expect(sameActiveContext(base, { ...base, source: 'unknown' })).toBe(false)
  })
})

describe('shouldPreserveRouteAcrossGatewaySwitch', () => {
  it('keeps a route overlay open across the switch', () => {
    // `/settings`, `/`, and contributed pages all parse to a null session id.
    expect(shouldPreserveRouteAcrossGatewaySwitch(null)).toBe(true)
    expect(shouldPreserveRouteAcrossGatewaySwitch(undefined)).toBe(true)
  })

  it('drops a route that names a session on the backend being left', () => {
    expect(shouldPreserveRouteAcrossGatewaySwitch('chat-a')).toBe(false)
  })

  it('treats a blank id as no session rather than a session named ""', () => {
    expect(shouldPreserveRouteAcrossGatewaySwitch('   ')).toBe(true)
  })
})

describe('activeContextLabels', () => {
  it('paints profile AND machine — identity must not be tooltip-only', () => {
    const labels = activeContextLabels(
      { connectionId: 'homelab', profile: 'research', source: 'session', storedSessionId: 'chat-a' },
      'Homelab'
    )

    expect(labels.text).toBe('research · Homelab')
    expect(labels.detail).toContain('research')
    expect(labels.detail).toContain('Homelab')
  })

  it('spells unknowns out instead of dropping the half it cannot derive', () => {
    const labels = activeContextLabels(
      { connectionId: null, profile: null, source: 'unknown', storedSessionId: 'chat-a' },
      null
    )

    expect(labels.text).toBe('unknown profile · unknown device')
    expect(labels.detail).toContain('owner unknown')
  })

  it('states where a draft would land', () => {
    const labels = activeContextLabels(
      { connectionId: 'work', profile: 'default', source: 'draft', storedSessionId: null },
      'Work laptop'
    )

    expect(labels.text).toBe('default · Work laptop')
    expect(labels.detail.startsWith('New chat:')).toBe(true)
  })

  it('falls back to the connection id when the registry cannot name it', () => {
    // A raw id still tells two machines apart, which is the job.
    const labels = activeContextLabels(
      { connectionId: 'conn-7', profile: 'p', source: 'session', storedSessionId: 's' },
      null
    )

    expect(labels.text).toBe('p · conn-7')
  })

  it('never produces empty visible text', () => {
    for (const source of ['draft', 'session', 'unknown'] as const) {
      const labels = activeContextLabels(
        { connectionId: null, profile: null, source, storedSessionId: null },
        null
      )

      expect(labels.text.trim().length).toBeGreaterThan(0)
    }
  })
})

describe('a session route does not survive a connection switch', () => {
  it('drops the id of the backend being left, keeps an overlay', () => {
    // `beforeConnectionSwitch` feeds routeSessionId(pathname) through this
    // policy: `/settings` and `/` parse to null (preserved), a session route
    // does not (dropped), so the wipe cannot leave the URL naming a session on
    // a backend that is gone while $sessions is empty.
    expect(shouldPreserveRouteAcrossGatewaySwitch(routeSessionId(SETTINGS_ROUTE))).toBe(true)
    expect(shouldPreserveRouteAcrossGatewaySwitch(routeSessionId(NEW_CHAT_ROUTE))).toBe(true)
    expect(shouldPreserveRouteAcrossGatewaySwitch(routeSessionId('/chat-a'))).toBe(false)
  })
})
