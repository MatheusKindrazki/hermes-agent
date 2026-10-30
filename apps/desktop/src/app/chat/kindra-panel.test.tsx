import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { KindraPanel } from './kindra-panel'
import { PANEL_ORIGIN, panelMessage } from './kindra-panel-protocol'

vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: () => true }))
const id = '11111111-2222-4333-8444-555555555555'
const url = `https://mac-mini-de-matheus.tail0673c.ts.net:8520/conversations/${id}`
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

it('accepts only the embedded panel and canonical conversation paths', () => {
  const guest = window
  const data = { type: 'kd-open-conversation', path: `/conversations/${id}?tab=events` }
  const event = { source: guest, origin: PANEL_ORIGIN, data }
  expect(panelMessage(event, guest)).toEqual({ type: 'conversation', url })

  for (const section of ['conversa', 'estudio']) {
    const hash = '#/' + section + '/' + id
    expect(panelMessage({ ...event, data: { type: 'kd-route', hash } }, guest)).toEqual({ type: 'conversation', url, hash })
  }

  expect(panelMessage({ ...event, origin: 'https://example.com' }, guest)).toBeNull()
  expect(panelMessage({ ...event, source: null }, guest)).toBeNull()
  expect(panelMessage(event, null)).toBeNull()

  for (const path of ['//example.com', '/conversations/../settings', '/conversations/not-a-uuid', '/conversations/' + id + '/events', 'javascript:alert(1)']) {
    expect(panelMessage({ ...event, data: { ...data, path } }, guest)).toBeNull()
  }
})

it('opens the selected conversation in Em paralelo, preserves the panel, and requires an explicit send', async () => {
  const read = vi.fn(async () => ({ kind: 'conversation', id, title: 'Engenharia · tarefa de teste', status: 'RUNNING', items: [{ role: 'agent', text: 'Verificando a tarefa.', at: '2026-10-29T21:00:00Z' }], fetchedAt: new Date().toISOString() }))
  const send = vi.fn()
  vi.stubGlobal('hermesDesktop', { readParallelExecutor: read, sendParallelExecutor: send })
  render(<KindraPanel />)
  expect(read).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: /^Painel$/ }))
  const frame = screen.getByTitle('Kindra Painel') as HTMLIFrameElement
  expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-forms')
  const message = (data: unknown, origin = PANEL_ORIGIN) => act(() => window.dispatchEvent(new MessageEvent('message', { source: frame.contentWindow, origin, data })))
  message({ type: 'kd-hello' })
  await waitFor(() => expect(screen.queryByLabelText('Abrindo o Painel…')).toBeNull())
  message({ type: 'kd-open-conversation', path: `/conversations/${id}` }, 'https://example.com')
  expect(read).not.toHaveBeenCalled()
  message({ type: 'kd-open-conversation', path: `/conversations/${id}` })
  await screen.findByText('Verificando a tarefa.')
  expect(read).toHaveBeenCalledWith(url)
  expect(screen.getByRole('heading', { name: 'Em paralelo' })).toBeTruthy()
  expect(screen.getByTitle('Kindra Painel')).toBe(frame)
  expect(send).not.toHaveBeenCalled()
  fireEvent.keyDown(screen.getByRole('heading', { name: 'Em paralelo' }), { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'Em paralelo' })).toBeNull())
  expect(screen.getByTitle('Kindra Painel')).toBe(frame)
})

it('offers a retry when the panel cannot handshake and discards messages from the previous frame', async () => {
  vi.useFakeTimers()
  render(<KindraPanel />)
  fireEvent.click(screen.getByRole('button', { name: /^Painel$/ }))
  const oldFrame = screen.getByTitle('Kindra Painel') as HTMLIFrameElement
  const oldGuest = oldFrame.contentWindow
  act(() => vi.advanceTimersByTime(15000))
  expect(screen.getByText('Não foi possível conectar ao Painel do Mac mini.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Tentar novamente' }))
  const frame = screen.getByTitle('Kindra Painel') as HTMLIFrameElement
  expect(frame).not.toBe(oldFrame)
  act(() => window.dispatchEvent(new MessageEvent('message', { source: oldGuest, origin: PANEL_ORIGIN, data: { type: 'kd-hello' } })))
  expect(screen.getByLabelText('Abrindo o Painel…')).toBeTruthy()
  act(() => window.dispatchEvent(new MessageEvent('message', { source: frame.contentWindow, origin: PANEL_ORIGIN, data: { type: 'kd-hello' } })))
  expect(screen.queryByLabelText('Abrindo o Painel…')).toBeNull()
})
