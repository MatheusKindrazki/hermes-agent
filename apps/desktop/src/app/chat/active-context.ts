import type { SessionOwnerScope } from '@/store/session-request-router'

/**
 * Who owns the destination of the next message: profile, connection, session.
 *
 * Derived, never stored. Every field is recomputed from the same resolvers the
 * submit path uses, so what the user reads cannot drift from where the text
 * actually lands. Nothing here is a pointer that can dangle, be stolen, or be
 * read back later — that is the failure class the removed session-id pin
 * introduced, and a value recomputed each render cannot reproduce it.
 */
export interface ActiveContext {
  /** The owning connection, or null when it cannot be derived — never guessed. */
  connectionId: null | string
  /** The owning profile, or null when it cannot be derived. */
  profile: null | string
  /**
   * `draft` — no session yet; the next message OPENS one on this route.
   * `session` — an existing conversation whose owner is known.
   * `unknown` — a session whose owner cannot be derived right now.
   */
  source: 'draft' | 'session' | 'unknown'
  /** null on a draft. */
  storedSessionId: null | string
}

export interface ActiveContextDeps {
  /** `resolveNewChatOwnerRoute()` — where a draft would be created. */
  newChatRoute: null | { connectionId: string; profile: string }
  /** `knownSessionOwner($sessions, targetStoredSessionId)`. */
  owner: SessionOwnerScope
  /**
   * The session the next message targets. Callers pass
   * `routeSessionId(pathname) ?? selectedStoredSessionId` — **route first**,
   * matching `resolve-target-session.ts`, because the route is the send
   * authority. Reading the selected id instead is exactly how the visible
   * identity and the real destination came apart.
   */
  targetStoredSessionId: null | string
}

const trimmed = (value: null | string | undefined): null | string => value?.trim() || null

/**
 * Resolve the active context. Pure: no stores, no React, no I/O.
 *
 * An underivable owner reports `unknown` rather than falling back to the
 * ambient connection/profile. Painting the window's current backend over a
 * session it may not own is a confident lie, and a lie about which machine is
 * about to receive the message is worse than "unknown" — a different backend
 * can recycle stored ids, so the ambient guess is not merely imprecise, it can
 * name the wrong machine's conversation.
 */
export function resolveActiveContext({
  newChatRoute,
  owner,
  targetStoredSessionId
}: ActiveContextDeps): ActiveContext {
  const storedSessionId = trimmed(targetStoredSessionId)

  if (!storedSessionId) {
    return {
      connectionId: trimmed(newChatRoute?.connectionId),
      profile: trimmed(newChatRoute?.profile),
      source: 'draft',
      storedSessionId: null
    }
  }

  // An exact owner: the row (or hint) carried its connection.
  if (owner && typeof owner === 'object' && 'connectionId' in owner) {
    return {
      connectionId: trimmed(owner.connectionId),
      profile: trimmed(owner.targetProfile) ?? trimmed(owner.profile),
      source: 'session',
      storedSessionId
    }
  }

  // A bare profile name. Known, but NOT an identity on its own: two sources
  // commonly both expose a `default` profile, so the connection stays null
  // instead of borrowing the ambient one.
  const bareProfile = typeof owner === 'string' ? trimmed(owner) : null

  if (bareProfile) {
    return { connectionId: null, profile: bareProfile, source: 'session', storedSessionId }
  }

  return { connectionId: null, profile: null, source: 'unknown', storedSessionId }
}

/** Whether two contexts name the same destination (for change detection). */
export function sameActiveContext(a: ActiveContext, b: ActiveContext): boolean {
  return (
    a.connectionId === b.connectionId &&
    a.profile === b.profile &&
    a.source === b.source &&
    a.storedSessionId === b.storedSessionId
  )
}

/**
 * Whether a gateway/connection switch may keep the current URL.
 *
 * `preserveRoute` exists so a switch does not close a route OVERLAY the user
 * is standing in (Settings, Gateway) — that is the entire reason the wipe
 * avoids navigating. It must not also preserve a route that names a SESSION:
 *
 * - that id belongs to the backend being left, and the switch empties
 *   `$sessions`, so nothing on screen can state which backend it meant;
 * - the route outranks the live runtime when the next message is targeted, so
 *   the send goes to a stale id while the tab reads "New session";
 * - the self-heal is latched off (the wipe sets `freshDraftReady`), so it does
 *   not recover on its own;
 * - and a backend that recycles stored ids turns the stuck loader into a
 *   message delivered into another machine's conversation.
 *
 * Takes the already-parsed routed session id (`routeSessionId(pathname)`) so
 * every non-session route — `/`, `/settings`, a contributed page — arrives here
 * as `null` and is preserved.
 */
export function shouldPreserveRouteAcrossGatewaySwitch(routedSessionId: null | string | undefined): boolean {
  return !trimmed(routedSessionId)
}

export interface ActiveContextLabels {
  /** The full sentence, for the tooltip and the accessible name. */
  detail: string
  /** Always-visible compact text, e.g. `research · Homelab`. Never empty. */
  text: string
}

const UNKNOWN_PROFILE = 'unknown profile'
const UNKNOWN_DEVICE = 'unknown device'

/**
 * Compose what the chip SHOWS and what it says in full.
 *
 * `text` is deliberately not optional and never empty: identity that lives
 * only in a tooltip or an aria-label is identity the user has to go looking
 * for, and the whole complaint is that they cannot tell at a glance which
 * profile and which machine the next message reaches. A hover is not a glance.
 *
 * Unknowns are spelled out rather than omitted. Dropping the half we cannot
 * derive would read as "there is nothing to say here", which is the opposite
 * of the truth.
 *
 * `connectionLabel` is the registry's human name for the connection; callers
 * pass null when the registry cannot name it (then the id, if any, is used —
 * a raw id still tells two machines apart, which is the job).
 */
export function activeContextLabels(
  context: ActiveContext,
  connectionLabel: null | string
): ActiveContextLabels {
  const profile = context.profile ?? UNKNOWN_PROFILE
  const device = connectionLabel?.trim() || context.connectionId?.trim() || UNKNOWN_DEVICE
  const what =
    context.source === 'draft' ? 'New chat' : context.source === 'unknown' ? 'Chat' : 'This chat'
  const owner = context.source === 'unknown' ? `owner unknown — ${profile} on ${device}` : `${profile} on ${device}`

  return { detail: `${what}: ${owner}`, text: `${profile} · ${device}` }
}
