import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { ExternalLink, openLink } from '@/lib/external-link'
import { setAlwaysExternalLinks } from '@/store/external-links'

import { KindraPanel } from './kindra-panel'
import { openPanelLink, panelLinkRoute } from './kindra-panel-link'
import { PANEL_HOST_URL, PANEL_URL } from './kindra-panel-protocol'
import { PANEL_ROUTE_KEY } from './kindra-panel-state'

vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: vi.fn(() => true) }))
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.mocked(usePaneVisible).mockReturnValue(true)
  setAlwaysExternalLinks(false)
  window.localStorage.removeItem(PANEL_ROUTE_KEY)
})

it('opens actual chat links in the panel at the requested route, including after another route was saved', async () => {
  const external = vi.fn()
  vi.stubGlobal('hermesDesktop', { openExternal: external })
  setAlwaysExternalLinks(true)
  window.localStorage.setItem(PANEL_ROUTE_KEY, '#/tarefas')
  render(<><KindraPanel /><ExternalLink href={PANEL_HOST_URL + '#/producao'}>Ver produção</ExternalLink></>)
  fireEvent.click(screen.getByRole('link', { name: 'Ver produção' }))
  expect(screen.getByTitle('Kindra Painel').getAttribute('src')).toBe(PANEL_URL + '#/producao')
  expect(external).not.toHaveBeenCalled()

  act(() => openLink(PANEL_HOST_URL + '#/chief/request-123'))
  const frame = screen.getByTitle('Kindra Painel')
  expect(frame.getAttribute('src')).toBe(PANEL_URL + '#/chief/request-123')
  fireEvent.keyDown(frame, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByTitle('Kindra Painel')).toBeNull())
  fireEvent.click(screen.getByRole('button', { name: /^Painel$/ }))
  expect(screen.getByTitle('Kindra Painel').getAttribute('src')).toBe(PANEL_URL + '#/chief/request-123')
})

it('retains explicit browser intent and fallback without a panel, and rejects lookalike or unsafe destinations', () => {
  const external = vi.fn()
  vi.stubGlobal('hermesDesktop', { openExternal: external })
  setAlwaysExternalLinks(true)
  expect(openPanelLink(PANEL_HOST_URL + '#/producao')).toBe(false)
  vi.mocked(usePaneVisible).mockReturnValue(false)
  const hidden = render(<KindraPanel />)
  expect(openPanelLink(PANEL_HOST_URL + '#/producao')).toBe(false)
  hidden.unmount()
  vi.mocked(usePaneVisible).mockReturnValue(true)
  render(<KindraPanel />)
  openLink(PANEL_HOST_URL + '#/producao', { native: true })
  expect(external).toHaveBeenCalledWith(PANEL_HOST_URL + '#/producao')
  expect(screen.queryByTitle('Kindra Painel')).toBeNull()

  for (const url of [
    'https://example.com/#/producao',
    PANEL_HOST_URL.replace('.ts.net', '.ts.net.evil.test') + '#/producao',
    PANEL_HOST_URL + '/other#/producao',
    PANEL_HOST_URL + '?redirect=evil#/producao',
    PANEL_HOST_URL.replace('https://', 'https://user@') + '#/producao',
    PANEL_HOST_URL + '#/bad?redirect=evil'
  ]) {
    expect(panelLinkRoute(url)).toBeNull()
    openLink(url)
    expect(external).toHaveBeenLastCalledWith(url)
  }

  expect(panelLinkRoute(PANEL_URL + '#/producao')).toBe('#/producao')
  expect(screen.queryByTitle('Kindra Painel')).toBeNull()
})
