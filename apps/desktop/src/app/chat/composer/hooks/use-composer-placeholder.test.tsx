import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { useComposerPlaceholder } from './use-composer-placeholder'

afterEach(cleanup)

it('names the routed recipient immediately even while its runtime is switching', () => {
  const { result, rerender } = renderHook(props => useComposerPlaceholder(props), {
    initialProps: { disabled: false, reconnecting: false, sessionId: 'runtime-chief', recipientName: 'Chief' }
  })

  expect(result.current).toBe('Message Chief')
  rerender({ disabled: true, reconnecting: false, sessionId: 'runtime-chief', recipientName: 'Writer' })
  expect(result.current).toBe('Message Writer')
  rerender({ disabled: true, reconnecting: false, sessionId: 'runtime-chief', recipientName: 'Chief' })
  expect(result.current).toBe('Message Chief')
  rerender({ disabled: false, reconnecting: false, sessionId: 'runtime-writer', recipientName: 'Writer' })
  expect(result.current).toBe('Message Writer')
})

it('updates persona copy without a session change or roster timer', () => {
  const { result, rerender } = renderHook(props => useComposerPlaceholder(props), {
    initialProps: { disabled: false, reconnecting: false, sessionId: 'same-runtime', recipientName: 'Old name' }
  })

  rerender({ disabled: false, reconnecting: false, sessionId: 'same-runtime', recipientName: 'New name' })
  expect(result.current).toBe('Message New name')
})
