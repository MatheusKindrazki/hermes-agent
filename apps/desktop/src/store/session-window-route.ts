import { routeSessionId } from '@/app/routes'

/** The session id the live HashRouter route names, or null when the route has
 *  no session opinion (new-chat draft, reserved/overlay/contributed page, or
 *  no hash at all). Desktop mounts HashRouter, so the app route lives in
 *  `location.hash` (`#/stored-A`); `location.pathname` is always the
 *  document's own path and never carries the session segment. */

export function windowRouteSessionId(): string | null {
  if (typeof window === 'undefined') {
    return null
  }

  return routeSessionId(window.location.hash.replace(/^#/, ''))
}
