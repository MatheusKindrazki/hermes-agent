import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '@/lib/chat-messages'
import { parallelLinks } from './parallel-links'

const message = (text: string, extras: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'one',
  role: 'assistant',
  parts: [{ type: 'text', text }],
  ...extras
})

describe('parallel task references', () => {
  it('keeps newest references and gives table rows task titles, not counts or link captions', () => {
    const url = 'https://mini.example/acompanhar/personal/kd-123/'
    const links = parallelLinks([
      message('| Tarefa antiga | QA | 2 | [acompanhar](' + url + ') |'),
      message('| Tarefa nova | Engenharia | 4 | [acompanhar](' + url + ') |')
    ])
    expect(links).toEqual([{ url, title: 'Tarefa nova', kind: 'task' }])
  })
  it('uses the preceding task heading for a conversation link and excludes unrelated/hidden/user links', () => {
    const url = 'https://mini.example/conversations/123?backend=abc'
    expect(
      parallelLinks([
        message('[Pessoal] PR pronto: Busca global\n[abrir conversa](' + url + ')'),
        message('[PR](https://github.com/repo/pull/1)'),
        message('[x](https://mini.example/conversations/private)', { hidden: true }),
        message('[x](https://mini.example/conversations/user)', { role: 'user' }),
        message('[x](https://user:password@mini.example/conversations/secret)')
      ])
    ).toEqual([{ url, title: '[Pessoal] PR pronto: Busca global', kind: 'conversation' }])
  })
})
