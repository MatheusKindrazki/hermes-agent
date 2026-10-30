import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $lastRoster } from '@/plugins/hermes-bots/data'
import type { RosterRow } from '@/plugins/hermes-bots/types'
import { recordExecutingTool } from '@/store/session-executing-tools'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { ChatPersonaHeader, conversationBot, useChatPersona, useRoutedChatPersona } from './bot-persona'

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

it('changes recipient identity synchronously without a roster refresh', () => {
  $lastRoster.set([
    { name: 'default', title: 'Chief', canonical_session: { id: 'chief' } },
    { name: 'writer', title: 'Writer', canonical_session: { id: 'writer' } }
  ])

  function Recipient({ id }: { id: string }) {
    return <span>{useChatPersona(id)?.title}</span>
  }

  const { rerender } = render(<Recipient id="chief" />)
  expect(screen.getByText('Chief')).toBeTruthy()
  rerender(<Recipient id="writer" />)
  expect(screen.getByText('Writer')).toBeTruthy()
  expect(screen.queryByText('Chief')).toBeNull()
  rerender(<Recipient id="chief" />)
  expect(screen.getByText('Chief')).toBeTruthy()
  expect(screen.queryByText('Writer')).toBeNull()
})


it('uses the primary route immediately, keeps tile ownership, and clears new-chat identity', () => {
  $lastRoster.set([
    { name: 'default', title: 'Chief', canonical_session: { id: 'chief' } },
    { name: 'writer', title: 'Writer', canonical_session: { id: 'writer' } }
  ])

  function Recipient({ primary, pathname }: { primary: boolean; pathname: string }) {
    return <span>{useRoutedChatPersona(primary, pathname, 'chief')?.title ?? 'New chat'}</span>
  }

  const { rerender } = render(<Recipient pathname="/writer" primary />)
  expect(screen.getByText('Writer')).toBeTruthy()
  rerender(<Recipient pathname="/writer" primary={false} />)
  expect(screen.getByText('Chief')).toBeTruthy()
  rerender(<Recipient pathname="/" primary />)
  expect(screen.getByText('New chat')).toBeTruthy()
})
