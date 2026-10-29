import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
vi.mock('@/components/pane-shell/pane-visibility', () => ({ usePaneVisible: () => true }))
import { useExecutorRead } from './parallel-executor-view'
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})
const result = {
  kind: 'conversation',
  id: 'a',
  title: 'A',
  status: 'RUNNING',
  items: [],
  fetchedAt: new Date().toISOString()
}
it('refreshes every five seconds, keeps last data on failure and stops after closing', async () => {
  vi.useFakeTimers()
  const read = vi.fn().mockResolvedValue(result)
  window.hermesDesktop = { ...window.hermesDesktop, readParallelExecutor: read }
  const { result: hook, unmount } = renderHook(() => useExecutorRead('a'))
  await act(async () => {})
  expect(hook.current.data).toEqual(result)
  read.mockRejectedValue(new Error('offline'))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000)
  })
  expect(hook.current.error).toBe(true)
  expect(hook.current.data).toEqual(result)
  unmount()
  const count = read.mock.calls.length
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000)
  })
  expect(read).toHaveBeenCalledTimes(count)
})
it('ignores a late response from the previous conversation', async () => {
  let resolve!: (value: unknown) => void
  const read = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise(r => {
          resolve = r
        })
    )
    .mockResolvedValue({ ...result, id: 'b' })
  window.hermesDesktop = { ...window.hermesDesktop, readParallelExecutor: read }
  const { result: hook, rerender } = renderHook(({ url }) => useExecutorRead(url), { initialProps: { url: 'a' } })
  rerender({ url: 'b' })
  await waitFor(() => expect((hook.current.data as any)?.id).toBe('b'))
  await act(async () => resolve(result))
  expect((hook.current.data as any)?.id).toBe('b')
})
it('does not request while disabled', () => {
  const read = vi.fn()
  window.hermesDesktop = { ...window.hermesDesktop, readParallelExecutor: read }
  renderHook(() => useExecutorRead('a', false))
  expect(read).not.toHaveBeenCalled()
})
