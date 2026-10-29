import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { ConnectorExecution } from './connector-tool'

vi.mock('@/components/assistant-ui/tool/fallback', () => ({
  ToolFallback: () => <div>Execution detail</div>
}))
afterEach(cleanup)
const args = { hermes_tool_labels: [{ app: 'delivery', text: 'Delivery', name: 'ask', kind: 'tool' }] }
function props(result: unknown, isError = false): ToolCallMessagePartProps {
  return {
    args,
    argsText: JSON.stringify(args),
    result,
    isError,
    type: 'tool-call',
    toolName: 'tool_call',
    toolCallId: 'test',
    status: { type: 'complete' },
    addResult: vi.fn(),
    resume: vi.fn(),
    respondToApproval: vi.fn()
  } as ToolCallMessagePartProps
}
describe('execution disclosure', () => {
  it('collapses successful calls with details available', () => {
    const { container } = render(<ConnectorExecution {...props({ ok: true })} />)
    expect(container.querySelector('details')).not.toBeNull()
    expect(container.querySelector('details')?.open).toBe(false)
    expect(container.querySelector('summary')?.textContent).toBe('Delivery')
    expect(container.textContent).toContain('Execution detail')
  })
  it('keeps pending calls and errors visible', () => {
    for (const part of [
      props(undefined),
      props({ error: 'failed' }),
      props({}, true),
      { ...props({ ok: true }), status: { type: 'running' } as const }
    ]) {
      const { container, unmount } = render(<ConnectorExecution {...part} />)
      expect(container.querySelector('details')).toBeNull()
      expect(container.textContent).toContain('Execution detail')
      unmount()
    }
  })
})
