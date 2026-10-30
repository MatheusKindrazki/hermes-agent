/** The existing Mini panel is a guest, never a desktop capability owner. */
export const PANEL_ORIGIN = 'https://mac-mini-de-matheus.tail0673c.ts.net:8525'
export const PANEL_URL = `${PANEL_ORIGIN}/painel/`
export const PANEL_HOST_URL = 'https://mac-mini-de-matheus.tail0673c.ts.net:8520/extensions/kindra-painel/painel'
const CONVERSATION_ORIGIN = 'https://mac-mini-de-matheus.tail0673c.ts.net:8520'
const CONVERSATION_PATH = /^\/conversations\/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\/?(?:\?[^#\s]*)?$/i

export type PanelMessage =
  | { type: 'ready' }
  | { type: 'conversation'; url: string; hash?: string }
  | { type: 'route'; hash: string }
  | { type: 'focus'; on: boolean }

export function panelMessage(event: Pick<MessageEvent, 'origin' | 'source' | 'data'>, guest: Window | null): PanelMessage | null {
  if (!guest || event.origin !== PANEL_ORIGIN || event.source !== guest) {
    return null
  }

  const data: unknown = event.data

  if (!data || typeof data !== 'object' || !('type' in data)) {
    return null
  }

  if (data.type === 'kd-hello') {
    return { type: 'ready' }
  }

  if (data.type === 'kd-open-conversation' && 'path' in data && typeof data.path === 'string') {
    const match = CONVERSATION_PATH.exec(data.path)

    return match ? { type: 'conversation', url: `${CONVERSATION_ORIGIN}/conversations/${match[1]}` } : null
  }

  if (data.type === 'kd-route' && 'hash' in data && typeof data.hash === 'string' && /^#\/[\w/-]{0,200}$/.test(data.hash)) {
    const path = data.hash.replace(/^#\/(conversa|estudio)\//, '/conversations/')
    const conversation = CONVERSATION_PATH.exec(path)

    if (conversation) {
      return { type: 'conversation', url: `${CONVERSATION_ORIGIN}/conversations/${conversation[1]}`, hash: data.hash }
    }

    return { type: 'route', hash: data.hash }
  }

  if (data.type === 'kd-focus' && 'on' in data && typeof data.on === 'boolean') {
    return { type: 'focus', on: data.on }
  }

  return null
}
