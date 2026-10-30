import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { useContextSubmitGuard } from './use-context-submit-guard'

vi.mock('@/api/capabilities', () => ({ getApiCapabilities: vi.fn() }))
afterEach(cleanup)

it('blocks submit and steer until route, view and runtime agree, including rapid A-B-A switches', () => {
  const onSubmit = vi.fn(() => true)
  const onSteer = vi.fn(() => true)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  const rowDefaults = { ended_at: null, input_tokens: 0, is_active: true, last_active: 1, model: null, output_tokens: 0, source: 'desktop', started_at: 1, tool_call_count: 0, title: 'Bot Chat', preview: null }

  const props: Parameters<typeof useContextSubmitGuard>[0] = {
    activeSessionId: 'runtime-a', routedSessionId: 'a', selectedSessionId: 'a', runtimeStoredSessionId: 'a',
    sessions: [{ ...rowDefaults, id: 'a', title: 'Bot Chat', profile: 'chief', connection_id: 'mini', message_count: 1 },
      { ...rowDefaults, id: 'b', title: 'Bot Chat', profile: 'writer', connection_id: 'mini', message_count: 1 }],
    activeConnectionId: 'mini', activeGatewayProfile: 'chief', gatewayOpen: true,
    identityV2Override: false, onSubmit, onSteer
  }

  const { result, rerender } = renderHook(useContextSubmitGuard, { initialProps: props, wrapper })
  expect(result.current.contextAllowsSubmit).toBe(true)
  rerender({ ...props, routedSessionId: 'b' })
  expect(result.current.composerDisabled).toBe(true)
  expect(result.current.submitWithContextGuard('wrong')).toBe(false)
  expect(result.current.steerWithContextGuard('wrong')).toBe(false)
  rerender({ ...props, routedSessionId: 'b', selectedSessionId: 'b' })
  expect(result.current.contextAllowsSubmit).toBe(false)
  rerender({ ...props, selectedSessionId: 'b', runtimeStoredSessionId: 'b', activeSessionId: 'runtime-b' })
  expect(result.current.submitWithContextGuard('late b')).toBe(false)
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onSteer).not.toHaveBeenCalled()
  rerender({ ...props, activeSessionId: null })
  expect(result.current.contextAllowsSubmit).toBe(false)
  rerender({ ...props, runtimeStoredSessionId: null })
  expect(result.current.contextAllowsSubmit).toBe(false)
  rerender(props)
  expect(result.current.submitWithContextGuard('chief')).toBe(true)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith('chief', undefined)
  rerender({ ...props, routedSessionId: 'root-a', sessions: [{ ...props.sessions[0], _lineage_root_id: 'root-a' }, props.sessions[1]] })
  expect(result.current.contextAllowsSubmit).toBe(true)
})
