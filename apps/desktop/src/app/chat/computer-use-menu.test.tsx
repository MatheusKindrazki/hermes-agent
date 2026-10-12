import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ComputerUseMenu } from './computer-use-menu'
const seen = vi.hoisted(() => ({ status: vi.fn(), config: vi.fn() }))
vi.mock('./bot-persona', () => ({ useChatPersona: () => ({ bot: { connectionLabel: 'Mac mini' } }) }))
vi.mock('@/plugins/hermes-bots/routing', () => ({
  resolveBotConnectionRoute: () => ({ route: { connectionId: 'mini-connection', profile: 'default' } })
}))
vi.mock('@/app/settings/computer-use-panel', () => ({
  ComputerUsePanel: (props: unknown) => {
    seen.status(props)

    return <span>Driver status</span>
  }
}))
vi.mock('@/app/settings/toolset-config-panel', () => ({
  ToolsetConfigPanel: (props: unknown) => {
    seen.config(props)

    return <span>Driver configuration</span>
  }
}))
describe('Computer Use entry', () => {
  it('opens on demand and pins both status and configuration to the conversation owner', () => {
    render(<ComputerUseMenu sessionId="canonical-chat" />)
    expect(seen.status).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Computer Use' }))
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText('Mac mini')).toBeTruthy()
    expect(seen.status).toHaveBeenLastCalledWith({ profile: { connectionId: 'mini-connection', profile: 'default' } })
    expect(seen.config).toHaveBeenLastCalledWith({
      profile: { connectionId: 'mini-connection', profile: 'default' },
      toolset: 'computer_use'
    })
  })
})
