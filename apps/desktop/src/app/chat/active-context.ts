import type { SessionOwnerScope } from '@/store/session-request-router'
import type { ActiveContextReceipt } from '@/types/hermes'

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
  /** Correlation contract version. Present on resolver-produced contexts. */
  schema?: 'kindra.active-context/v1'
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
  /** Backend tenant, never inferred from the profile name. */
  tenant?: null | string
  /** Stable machine identity/label for the owning connection. */
  machine?: null | string
  /** Runtime generation that minted the route/session. */
  gatewayGeneration?: null | string
  /** Live streaming identity; distinct from the durable stored id. */
  runtimeSessionId?: null | string
  xirpSessionId?: null | string
  workId?: null | string
  /** Stable machine-readable reasons for an unknown v2 context. */
  reasonCodes?: readonly string[]
}

export interface ActiveContextCorrelation {
  connectionId: null | string
  profile: null | string
  tenant: null | string
  machine: null | string
  gatewayGeneration: null | string
  runtimeSessionId: null | string
  storedSessionId: null | string
  xirpSessionId: null | string
  workId: null | string
}

/** Translate the backend receipt without filling any identity field locally. */
export function activeContextCorrelationFromReceipt(
  receipt: ActiveContextReceipt | null | undefined
): ActiveContextCorrelation | null {
  if (receipt?.schema !== 'kindra.active-context/v1') {
    return null
  }

  return {
    connectionId: trimmed(receipt.connection_id),
    profile: trimmed(receipt.profile),
    tenant: trimmed(receipt.tenant),
    machine: trimmed(receipt.machine),
    gatewayGeneration: trimmed(receipt.gateway_generation),
    runtimeSessionId: trimmed(receipt.runtime_session_id),
    storedSessionId: trimmed(receipt.stored_session_id),
    xirpSessionId: trimmed(receipt.xirp_session_id),
    workId: trimmed(receipt.work_id)
  }
}

export interface ActiveContextDeps {
  /** The runtime currently mounted by this chat surface. */
  activeRuntimeSessionId?: null | string
  /** Backend/route correlation receipt. Required only while v2 is enabled. */
  correlation?: null | ActiveContextCorrelation
  /** Internal/default-off rollout gate. */
  identityV2?: boolean
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
  activeRuntimeSessionId,
  correlation,
  identityV2 = false,
  newChatRoute,
  owner,
  targetStoredSessionId
}: ActiveContextDeps): ActiveContext {
  const storedSessionId = trimmed(targetStoredSessionId)
  let base: ActiveContext

  if (!storedSessionId) {
    base = {
      connectionId: trimmed(newChatRoute?.connectionId),
      profile: trimmed(newChatRoute?.profile),
      source: 'draft',
      storedSessionId: null
    }
  } else if (owner && typeof owner === 'object' && 'connectionId' in owner) {
    // An exact owner: the row (or hint) carried its connection.
    base = {
      connectionId: trimmed(owner.connectionId),
      profile: trimmed(owner.targetProfile) ?? trimmed(owner.profile),
      source: 'session',
      storedSessionId
    }
  } else {
    // A bare profile name. Known, but NOT an identity on its own: two sources
    // commonly both expose a `default` profile, so the connection stays null
    // instead of borrowing the ambient one.
    const bareProfile = typeof owner === 'string' ? trimmed(owner) : null

    base = bareProfile
      ? { connectionId: null, profile: bareProfile, source: 'session', storedSessionId }
      : { connectionId: null, profile: null, source: 'unknown', storedSessionId }
  }

  const observed = correlation ?? null

  const result: ActiveContext = {
    ...base,
    schema: 'kindra.active-context/v1',
    tenant: trimmed(observed?.tenant),
    machine: trimmed(observed?.machine),
    gatewayGeneration: trimmed(observed?.gatewayGeneration),
    runtimeSessionId: trimmed(observed?.runtimeSessionId) ?? trimmed(activeRuntimeSessionId),
    xirpSessionId: trimmed(observed?.xirpSessionId),
    workId: trimmed(observed?.workId),
    reasonCodes: []
  }

  if (!identityV2) {
    return result
  }

  const reasons: string[] = []
  const observedConnection = trimmed(observed?.connectionId)
  const observedProfile = trimmed(observed?.profile)
  const observedStored = trimmed(observed?.storedSessionId)
  const observedRuntime = trimmed(observed?.runtimeSessionId)
  const activeRuntime = trimmed(activeRuntimeSessionId)

  if (!observed) {
    reasons.push('correlation_missing')
  }

  if (!base.connectionId || observedConnection !== base.connectionId) {
    reasons.push('connection_mismatch')
  }

  if (!base.profile || observedProfile !== base.profile) {
    reasons.push('profile_mismatch')
  }

  if (!result.tenant) {
    reasons.push('tenant_missing')
  }

  if (!result.machine) {
    reasons.push('machine_missing')
  }

  if (!result.gatewayGeneration) {
    reasons.push('gateway_generation_missing')
  }

  if (storedSessionId) {
    if (observedStored !== storedSessionId) {
      reasons.push('stored_session_mismatch')
    }

    if (!activeRuntime || observedRuntime !== activeRuntime) {
      reasons.push('runtime_session_mismatch')
    }
  } else if (observedStored || observedRuntime || activeRuntime) {
    reasons.push('draft_session_mismatch')
  }

  return reasons.length ? { ...result, source: 'unknown', reasonCodes: reasons } : { ...result, reasonCodes: [] }
}

/** Default-off submit gate: legacy mode never blocks; v2 fails closed. */
export function activeContextCanSubmit(context: ActiveContext, identityV2: boolean): boolean {
  return !identityV2 || (context.source !== 'unknown' && (context.reasonCodes?.length ?? 0) === 0)
}

/** Whether two contexts name the same destination (for change detection). */
export function sameActiveContext(a: ActiveContext, b: ActiveContext): boolean {
  return (
    a.connectionId === b.connectionId &&
    a.profile === b.profile &&
    a.source === b.source &&
    a.storedSessionId === b.storedSessionId &&
    (a.tenant ?? null) === (b.tenant ?? null) &&
    (a.machine ?? null) === (b.machine ?? null) &&
    (a.gatewayGeneration ?? null) === (b.gatewayGeneration ?? null) &&
    (a.runtimeSessionId ?? null) === (b.runtimeSessionId ?? null) &&
    (a.xirpSessionId ?? null) === (b.xirpSessionId ?? null) &&
    (a.workId ?? null) === (b.workId ?? null) &&
    (a.reasonCodes ?? []).join('\0') === (b.reasonCodes ?? []).join('\0')
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
  /** Four visible identity dimensions. Never empty. */
  text: string
}

const UNKNOWN_PROFILE = 'unknown profile'
const UNKNOWN_TENANT = 'unknown tenant'
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
export function activeContextLabels(context: ActiveContext, connectionLabel: null | string): ActiveContextLabels {
  const profile = context.profile ?? UNKNOWN_PROFILE
  const tenant = context.tenant?.trim() || UNKNOWN_TENANT
  const device = connectionLabel?.trim() || context.machine?.trim() || context.connectionId?.trim() || UNKNOWN_DEVICE

  const what = context.source === 'draft' ? 'New chat' : context.source === 'unknown' ? 'Chat' : 'This chat'

  const owner = context.source === 'unknown' ? `owner unknown — ${profile} on ${device}` : `${profile} on ${device}`

  return { detail: `${what}: ${owner}; tenant ${tenant}`, text: `${what} · ${profile} · ${tenant} · ${device}` }
}
