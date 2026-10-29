import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { I18nProvider, type Locale } from '@/i18n'
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
    const { container } = render(
      <ActiveContextChip
        context={{
          connectionId: 'this-mac',
          machine: 'personal-mac-mini',
          profile: 'default',
          source: 'draft',
          storedSessionId: null,
          tenant: 'kindra'
        }}
      />
    )

    const chip = container.querySelector('[data-slot="active-context-chip"]')

    expect(chip).toBeTruthy()
    expect(screen.getByText(/New chat/)).toBeTruthy()
    expect(screen.getByText(/default/)).toBeTruthy()
    expect(screen.getByText(/kindra/)).toBeTruthy()
    expect(screen.getByText(/This Mac/)).toBeTruthy()
    expect(chip?.getAttribute('data-active-context-conversation')).toBe('draft')
    expect(chip?.getAttribute('data-active-context-machine')).toBe('personal-mac-mini')
    expect(chip?.getAttribute('data-active-context-profile')).toBe('default')
    expect(chip?.getAttribute('data-active-context-tenant')).toBe('kindra')
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

  it.each<readonly [Locale, string]>([
    ['en', 'Chat · unknown profile · unknown tenant · unknown device'],
    ['ja', 'チャット · 不明なプロファイル · 不明なテナント · 不明なデバイス'],
    ['zh', '对话 · 未知配置档案 · 未知租户 · 未知设备'],
    ['zh-hant', '對話 · 未知設定檔 · 未知租戶 · 未知裝置'],
    ['ar', 'محادثة · ملف شخصي غير معروف · مستأجر غير معروف · جهاز غير معروف']
  ])('renders active-context copy through the %s locale catalog', (locale, expected) => {
    render(
      <I18nProvider configClient={null} initialLocale={locale}>
        <ActiveContextChip
          context={{ connectionId: null, profile: null, source: 'unknown', storedSessionId: 'chat-a' }}
        />
      </I18nProvider>
    )

    expect(screen.getByText(expected)).toBeTruthy()
  })
})
