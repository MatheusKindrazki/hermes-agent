import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocked = vi.hoisted(() => ({ request: vi.fn(), view: null as any }))
vi.mock('./session-view', () => ({ useSessionView: () => mocked.view }))
vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: () => true }))
vi.mock('@/store/session-states', () => ({
  knownOwnerForSession: () => ({ connectionId: 'mini', profile: 'personal' }),
  requestForOwnedSession: (...args: unknown[]) => mocked.request(...args)
}))
vi.mock('./composer/status-stack/subagent-transcript', () => ({
  SubagentTranscript: ({ sessionId, subagentId }: { sessionId: string; subagentId: string }) => (
    <div>
      Transcript {sessionId}/{subagentId}
    </div>
  )
}))
import { $subagentsBySession } from '@/store/subagents'
import { ParallelActivity } from './parallel-activity'

beforeEach(() => {
  mocked.view = { $runtimeId: atom('one'), $messages: atom([]) }
  mocked.request.mockReset()
  mocked.request.mockResolvedValue({ subagents: [] })
  $subagentsBySession.set({})
})
afterEach(cleanup)

it('does not query while closed, scopes tasks and transcript to this chat, and closes on session change', async () => {
  render(<ParallelActivity />)
  expect(mocked.request).not.toHaveBeenCalled()
  mocked.request.mockResolvedValue({
    subagents: [
      {
        subagent_id: 'child',
        goal: 'Check build',
        status: 'running',
        started_at: 1
      }
    ]
  })
  fireEvent.click(screen.getByRole('button', { name: 'Em paralelo' }))
  await screen.findByText('Check build')
  expect(mocked.request.mock.calls[0][0]).toBe('one')
  expect(mocked.request.mock.calls[0][3]).toEqual({ session_id: 'one' })
  fireEvent.click(screen.getByRole('button', { name: /Check build/ }))
  expect(screen.getByText('Transcript one/child')).toBeTruthy()
  act(() => mocked.view.$runtimeId.set('two'))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

it('shows a read error instead of claiming there are no tasks', async () => {
  mocked.request.mockRejectedValue(new Error('offline'))
  render(<ParallelActivity />)
  fireEvent.click(screen.getByRole('button', { name: 'Em paralelo' }))
  await screen.findByText(/Não foi possível atualizar/)
  expect(screen.queryByText('Nenhum subagente informado nesta conversa.')).toBeNull()
})
