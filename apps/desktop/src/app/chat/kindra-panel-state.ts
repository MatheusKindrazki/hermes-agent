import { readKey, writeKey } from '@/lib/storage'

import { isPanelRoute } from './kindra-panel-protocol'

export const PANEL_ROUTE_KEY = 'hermes.desktop.kindraPanel.route.v1'

/** Restore only a panel hash, never an arbitrary iframe URL. */
export function readPanelRoute(): string {
  const route = readKey(PANEL_ROUTE_KEY)

  return isPanelRoute(route) ? route : '#/plataforma'
}

export function rememberPanelRoute(route: string): void {
  if (isPanelRoute(route)) {
    writeKey(PANEL_ROUTE_KEY, route)
  }
}
