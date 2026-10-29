import { expect, it } from 'vitest'
import type { ChatMessage } from '@/lib/chat-messages'
import { agentExchanges } from './parallel-exchanges'
const msg = (id: string, role: 'user' | 'assistant', text: string): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text }]
})
it('shows incoming messages and their replies but does not classify a human answer as an agent exchange', () => {
  expect(
    agentExchanges([
      msg('1', 'user', 'Message from 🤖 Inbox (@inbox): Confirme o escopo'),
      msg('2', 'assistant', 'Escopo confirmado'),
      msg('3', 'user', 'Qual o andamento?'),
      msg('4', 'assistant', 'Tudo certo')
    ]).map(row => row.label)
  ).toEqual(['Resposta a Inbox', 'De Inbox'])
})
it('keeps an answer to our dispatch separate from the report back to the user', () => {
  const dispatch: ChatMessage = {
    id: 'a',
    role: 'assistant',
    parts: [
      {
        type: 'tool-call',
        toolCallId: 'call',
        toolName: 'message_agent',
        args: { target: 'inbox', message: 'Verifique o escopo' },
        argsText: '{}'
      }
    ]
  }
  expect(
    agentExchanges([
      dispatch,
      msg('b', 'user', 'Message from 🤖 Inbox (@inbox): Verificado'),
      msg('c', 'assistant', 'O Inbox confirmou o escopo')
    ]).map(row => row.label)
  ).toEqual(['De Inbox', 'Pedido para inbox'])
})
