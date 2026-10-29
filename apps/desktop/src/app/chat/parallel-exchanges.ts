import { AGENT_MESSAGE_RE } from '@/components/assistant-ui/thread/user-message'
import { dispatchedTo } from '@/components/assistant-ui/thread/agent-delivery'
import { chatMessageText, type ChatMessage } from '@/lib/chat-messages'

export interface AgentExchange {
  id: string
  label: string
  text: string
}

export function agentExchanges(messages: ChatMessage[]): AgentExchange[] {
  const rows: AgentExchange[] = []
  let replyTo: string | null = null
  for (let index = 0; index < messages.length; index++) {
    const message = messages[index]
    if (message.hidden) continue
    const text = chatMessageText(message).trim()
    if (message.role === 'user') {
      const match = AGENT_MESSAGE_RE.exec(text)
      replyTo = null
      if (match) {
        const sender = (match[1] || match[3] || 'Agente').trim()
        rows.push({ id: message.id, label: 'De ' + sender, text: match[4] })
        const earlier = messages.slice(0, index).map(row => ({ role: row.role, content: row.parts }))
        if (!dispatchedTo(earlier, [match[1], match[2], match[3]])) replyTo = sender
      }
    } else if (message.role === 'assistant') {
      if (replyTo && text) {
        rows.push({ id: message.id, label: 'Resposta a ' + replyTo, text })
        replyTo = null
      }
      for (const [partIndex, part] of message.parts.entries()) {
        if (part.type !== 'tool-call' || part.toolName !== 'message_agent') continue
        const args = part.args as { target?: unknown; message?: unknown }
        if (typeof args?.target === 'string' && typeof args.message === 'string') {
          rows.push({ id: message.id + ':' + partIndex, label: 'Pedido para ' + args.target, text: args.message })
        }
      }
    }
  }
  return rows.slice(-50).reverse()
}
