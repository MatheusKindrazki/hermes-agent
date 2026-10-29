import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $lastRoster } from '@/plugins/hermes-bots/data'
import type { RosterRow } from '@/plugins/hermes-bots/types'
import { recordExecutingTool } from '@/store/session-executing-tools'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { ChatPersonaHeader, conversationBot, useChatPersona } from './bot-persona'

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  $lastRoster.set([])
})

it('resolves only a unique canonical chat, including its compression tip', () => {
  const bot: RosterRow = {
    name: 'default',
    canonical_session: { id: 'root', resolved_id: 'tip' },
    last_session: { id: 'unrelated' }
  }

  expect(conversationBot([bot], 'root')).toBe(bot)
  expect(conversationBot([bot], 'tip')).toBe(bot)
  expect(conversationBot([bot], 'unrelated')).toBeNull()
  expect(conversationBot([bot, { ...bot, name: 'another' }], 'root')).toBeNull()
})

it('shows the persona and changes its avatar from thought bubble to tablet and input wait', () => {
  $lastRoster.set([{ name: 'default', title: 'Chief', canonical_session: { id: 'root' } }])
  const state = { ...createClientSessionState('root'), busy: true, turnLive: true }
  publishSessionState('runtime', state)

  function Header() {
    const persona = useChatPersona('root')

    return persona ? <ChatPersonaHeader persona={persona} /> : null
  }

  const result = render(<Header />)
  expect(screen.getByRole('heading', { name: 'Chief' })).toBeTruthy()
  expect(result.container.querySelector('[data-hb-thinking-bubble]')).toBeTruthy()
  act(() => recordExecutingTool('runtime', 'tool', 'terminal', true))
  expect(screen.getByText('Executando um comando')).toBeTruthy()
  expect(result.container.querySelector('[data-hb-tablet]')).toBeTruthy()
  act(() => publishSessionState('runtime', { ...state, needsInput: true }))
  expect(screen.getByText('Aguardando sua resposta')).toBeTruthy()
  expect(result.container.querySelector('[data-hb-tablet]')).toBeNull()
})

it('uses the same delegated-worker activity as the roster', () => {
  $lastRoster.set([
    {
      name: 'default',
      title: 'Chief',
      canonical_session: { id: 'root' },
      worker_session: { last_active: Date.now() / 1000 }
    }
  ])

  function Header() {
    const persona = useChatPersona('root')

    return persona ? <ChatPersonaHeader persona={persona} /> : null
  }

  const result = render(<Header />)
  expect(screen.getByText('Trabalhando em segundo plano')).toBeTruthy()
  expect(result.container.querySelector('[data-hb-tablet]')).toBeTruthy()
})
