import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getLatestSessionMessages, getSession } from '@/hermes'
import { $notifications } from '@/store/notifications'
import { $cronRunReadOnlyVerdicts } from '@/store/read-only-transcript'
import { setSessions } from '@/store/session'
import { actRender, Harness, type HarnessHandle, sessionInfo } from './index-test-utils'
import { clearSingleFlightSessionResumeState } from './single-flight-resume'

beforeEach(() => {
  clearSingleFlightSessionResumeState()
  window.localStorage.removeItem('hermes.desktop.preparedSubmissions.v1')
  // Queue mutations build on the persisted map, not the atom — a queue an
  // earlier test left in storage would otherwise sit ahead of this test's send.
  window.localStorage.removeItem('hermes.desktop.composerQueue.v1')
  vi.mocked(getLatestSessionMessages).mockReset()
  vi.mocked(getLatestSessionMessages).mockImplementation(async () => ({ messages: [], session_id: 'session' }))
})

vi.mock('@/hermes', () => ({
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: 'session' })),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS: 1_800_000,
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

describe('usePromptActions cron run write gate (#88443)', () => {
  const storedId = 'cron_job-1_20260929_120000'

  afterEach(() => {
    cleanup()
    $notifications.set([])
    $cronRunReadOnlyVerdicts.set(new Map())
    setSessions(() => [])
    vi.mocked(getSession).mockReset()
  })

  const renderRestoredRun = async (requestGateway: ReturnType<typeof vi.fn>) => {
    let handle: HarnessHandle | null = null

    // A route/tab restored after an app restart: no Cron surface evaluated the
    // run, so there is no verdict yet — the send itself must gate it.
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        refreshSessions={async () => undefined}
        requestGateway={requestGateway as never}
        storedSessionId={storedId}
      />
    )

    return handle!
  }

  it('refuses a send into a restored never-closed run the scheduler does not own', async () => {
    vi.mocked(getSession).mockResolvedValue(
      sessionInfo({ ended_at: null, id: storedId, scheduler_owned: false, source: 'cron' })
    )

    const requestGateway = vi.fn(async () => ({}) as never)
    const handle = await renderRestoredRun(requestGateway)

    expect(await handle.submitText('into the dead cron session')).toBe(false)
    expect(getSession).toHaveBeenCalledWith(storedId, expect.anything())
    expect(requestGateway).not.toHaveBeenCalledWith('prompt.submit', expect.anything(), expect.anything())
    expect($notifications.get().some(note => note.kind === 'info')).toBe(true)
  })

  it('sends into a run past the activity window while the scheduler still owns it', async () => {
    $cronRunReadOnlyVerdicts.set(new Map([[storedId, true]])) // looked idle earlier
    vi.mocked(getSession).mockResolvedValue(
      sessionInfo({ ended_at: null, id: storedId, is_active: false, scheduler_owned: true, source: 'cron' })
    )

    const requestGateway = vi.fn(async () => ({}) as never)
    const handle = await renderRestoredRun(requestGateway)

    expect(await handle.submitText('still running')).toBe(true)
    expect(requestGateway).toHaveBeenCalledWith(
      'prompt.submit',
      expect.objectContaining({ text: 'still running' }),
      1_800_000
    )
  })
})
