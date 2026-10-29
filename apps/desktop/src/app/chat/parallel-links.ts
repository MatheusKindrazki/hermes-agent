import { chatMessageText, type ChatMessage } from '@/lib/chat-messages'

export interface ParallelLink {
  url: string
  title: string
  kind: 'task' | 'conversation'
}

/** References already shared in this chat, not a claim about live task state. */
export function parallelLinks(messages: ChatMessage[]): ParallelLink[] {
  const found = new Map<string, ParallelLink>()
  for (const message of messages) {
    if (message.hidden || message.role !== 'assistant') continue
    let heading = ''
    for (const line of chatMessageText(message).split('\n')) {
      const matches = [...line.matchAll(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g)]
      for (const match of matches) {
        let url: URL
        try {
          url = new URL(match[2])
        } catch {
          continue
        }
        const kind = /^\/acompanhar\/[^/]+\/[^/]+\/?$/.test(url.pathname)
          ? 'task'
          : /^\/conversations\/[^/]+\/?$/.test(url.pathname)
            ? 'conversation'
            : null
        if (!kind || url.username || url.password) continue
        const plain = line
          .replace(/\[[^\]]+\]\([^)]+\)/g, '')
          .replace(/[*#]/g, '')
          .trim()
        const title =
          (line.includes('|')
            ? plain
                .split('|')
                .map(value => value.trim())
                .find(Boolean)
            : plain || heading || match[1]) || match[1]
        found.delete(url.href)
        found.set(url.href, { url: url.href, title: title.slice(0, 180), kind })
      }
      if (line.trim() && !matches.length && !line.startsWith('|')) {
        heading = line.replace(/[*#]/g, '').trim()
      }
    }
  }
  return [...found.values()].reverse().slice(0, 40)
}
