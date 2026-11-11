import { isPanelRoute, PANEL_HOST_URL, PANEL_URL } from './kindra-panel-protocol'

export const PANEL_OPEN_EVENT = 'hermes:kindra-panel-open'

/** Match the two owned panel entry points, never arbitrary URLs on the Mini. */
export function panelLinkRoute(href: string): string | null {
  try {
    const url = new URL(href)

    const known = [PANEL_HOST_URL, PANEL_URL].some(base => {
      const entry = new URL(base)

      return url.origin === entry.origin && url.pathname.replace(/\/$/, '') === entry.pathname.replace(/\/$/, '')
    })

    if (!known || url.username || url.password || url.search) {
      return null
    }

    return !url.hash ? '#/plataforma' : isPanelRoute(url.hash) ? url.hash : null
  } catch {
    return null
  }
}

/** Fall back to normal link handling when this window has no panel owner. */
export function openPanelLink(href: string): boolean {
  const route = panelLinkRoute(href)

  return route !== null && !window.dispatchEvent(new CustomEvent(PANEL_OPEN_EVENT, {
    detail: route, cancelable: true
  }))
}
