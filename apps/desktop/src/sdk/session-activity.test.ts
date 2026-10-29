import { afterEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $executingToolsByRuntime, recordExecutingTool } from '@/store/session-executing-tools'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { $sessionToolsById, $sessionWorkById } from './session-activity'

afterEach(() => {
  clearAllSessionStates()
  $executingToolsByRuntime.set({})
})

describe('activity for stored sessions without focused tiles', () => {
  it('keeps working across commentary boundaries and clears on stop or completion', () => {
    const state = {
      ...createClientSessionState('canonical-execution'),
      busy: true,
      turnLive: true,
      streamId: 'new-commentary'
    }
    publishSessionState('runtime-execution', state)
    recordExecutingTool('runtime-execution', 'one', 'browser', true)
    recordExecutingTool('runtime-execution', 'two', 'terminal', true)
    expect($sessionWorkById.get()['canonical-execution']).toBe('work')
    expect($sessionToolsById.get()['canonical-execution']).toEqual(['browser', 'terminal'])
    recordExecutingTool('runtime-execution', 'one', 'browser', false)
    expect($sessionWorkById.get()['canonical-execution']).toBe('work')
    recordExecutingTool('runtime-execution', 'two', 'terminal', false)
    expect($sessionWorkById.get()['canonical-execution']).toBe('think')
    recordExecutingTool('runtime-execution', 'three', 'terminal', true)
    publishSessionState('runtime-execution', { ...state, busy: false, turnLive: false })
    expect($executingToolsByRuntime.get()['runtime-execution']).toBeUndefined()
    expect($sessionWorkById.get()['canonical-execution']).not.toBe('work')
  })

  it('exports only pending tool names and keeps the map stable across unchanged operations', () => {
    const state = {
      ...createClientSessionState('tools-canonical'),
      busy: true,
      streamId: 'reply',
      messages: [
        {
          id: 'reply',
          role: 'assistant' as const,
          pending: true,
          parts: [
            {
              type: 'tool-call' as const,
              toolCallId: 'call',
              toolName: 'read_file',
              args: { confidential: 'synthetic-private-argument' }
            }
          ]
        }
      ]
    }

    publishSessionState('runtime-tools', state)
    const first = $sessionToolsById.get()
    expect(first['tools-canonical']).toEqual(['read_file'])
    expect(JSON.stringify(first)).not.toContain('synthetic-private-argument')
    publishSessionState('runtime-tools', { ...state, sawAssistantPayload: true })
    expect($sessionToolsById.get()).toBe(first)
    publishSessionState('runtime-tools', { ...state, busy: false })
    expect($sessionToolsById.get()['tools-canonical']).toBeUndefined()
  })

  it('tracks thinking, executing tools, waiting and completion on the same stored id', () => {
    const state = { ...createClientSessionState('background-canonical'), busy: true, turnLive: true, streamId: 'reply' }
    publishSessionState('runtime-background', state)
    expect($sessionWorkById.get()['background-canonical']).toBe('think')
    publishSessionState('runtime-background', {
      ...state,
      messages: [
        {
          id: 'reply',
          role: 'assistant',
          pending: true,
          parts: [{ type: 'tool-call', toolCallId: 'call', toolName: 'terminal', args: {} }]
        }
      ]
    })
    expect($sessionWorkById.get()['background-canonical']).toBe('work')
    publishSessionState('runtime-background', { ...state, needsInput: true })
    expect($sessionWorkById.get()['background-canonical']).toBe('waiting')
    publishSessionState('runtime-background', { ...state, busy: false, turnLive: false })
    expect(['idle', undefined]).toContain($sessionWorkById.get()['background-canonical'])
  })
})
