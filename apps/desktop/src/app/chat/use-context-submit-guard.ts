import { useQuery } from '@tanstack/react-query'
import { useCallback, useMemo } from 'react'
import { getApiCapabilities } from '@/api/capabilities'
import type { SubmitTextOptions } from '@/app/session/hooks/use-prompt-actions/utils'
import { resolveNewChatOwnerRoute } from '@/store/profile'
import { $sessions, knownSessionOwner, sessionMatchesStoredId } from '@/store/session'
import { activeContextCanSubmit, activeContextCapabilityDecision, activeContextCorrelationFromReceipt, resolveActiveContext } from './active-context'

interface ContextSubmitGuardOptions {
  activeSessionId: string | null
  routedSessionId: string | null
  selectedSessionId: string | null
  sessions: ReturnType<typeof $sessions.get>
  activeConnectionId: string | null
  activeGatewayProfile: string
  gatewayOpen: boolean
  identityV2Override: boolean | undefined
  onSubmit: (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean
  onSteer: (text: string) => Promise<boolean> | boolean
}

function contextOwnerRoute(
  owner: ReturnType<typeof knownSessionOwner>,
  targetSession: ContextSubmitGuardOptions['sessions'][number] | null,
  draftRoute: ReturnType<typeof resolveNewChatOwnerRoute>,
  activeConnectionId: string | null,
  activeGatewayProfile: string
) {
  const ownerConnectionId =
    owner && typeof owner === 'object' && 'connectionId' in owner
      ? owner.connectionId
      : targetSession?.connection_id || draftRoute?.connectionId || activeConnectionId

  const ownerProfile =
    owner && typeof owner === 'object' && 'connectionId' in owner
      ? owner.targetProfile || owner.profile
      : typeof owner === 'string'
        ? owner
        : targetSession?.profile || draftRoute?.profile || activeGatewayProfile

  return { ownerConnectionId, ownerProfile }
}

export function useContextSubmitGuard({ activeSessionId, routedSessionId, selectedSessionId, sessions,
  activeConnectionId, activeGatewayProfile, gatewayOpen, identityV2Override, onSubmit, onSteer
}: ContextSubmitGuardOptions) {
  const targetStoredSessionId = routedSessionId ?? selectedSessionId

  const targetSession = targetStoredSessionId
    ? (sessions.find(session => sessionMatchesStoredId(session, targetStoredSessionId)) ?? null)
    : null

  const owner = knownSessionOwner(sessions, targetStoredSessionId)

  const draftRoute = useMemo(() => {
    void activeConnectionId
    void activeGatewayProfile

    return resolveNewChatOwnerRoute()
  }, [activeConnectionId, activeGatewayProfile])

  const { ownerConnectionId, ownerProfile } = contextOwnerRoute(owner, targetSession, draftRoute, activeConnectionId, activeGatewayProfile)

  const activeContextCapabilityQuery = useQuery({
    queryKey: ['active-context-capability', ownerConnectionId ?? 'local', ownerProfile ?? 'default'],
    queryFn: () =>
      getApiCapabilities({
        connectionId: ownerConnectionId,
        profile: ownerProfile
      }),
    enabled: gatewayOpen && identityV2Override === undefined,
    retry: false,
    staleTime: 30_000
  })

  const activeContextCapability = activeContextCapabilityQuery.data?.features?.active_context_v2

  // Capability and receipt are separate authority signals. A canary-enabled
  // backend can legitimately have no receipt (missing identity config, draft,
  // malformed row); that absence must block rather than silently falling back
  // to legacy routing. Tests may force the gate through the internal prop.
  const activeContextCapabilityDecisionResult = activeContextCapabilityDecision({
    feature: activeContextCapability,
    override: identityV2Override,
    status: activeContextCapabilityQuery.status
  })

  const identityV2Enabled = activeContextCapabilityDecisionResult.v2

  const activeContext = useMemo(() => {
    // The legacy local door intentionally returns no explicit draft route:
    // its submit is ambient. The ambient destination is still exact at this
    // render because these two atoms are the same active connection/profile
    // the request dispatcher uses. Carry that pair for display only; never
    // infer either field from a title, session id, or profile name.
    const displayedDraftRoute =
      draftRoute ??
      (!targetStoredSessionId && ownerConnectionId && ownerProfile
        ? { connectionId: ownerConnectionId, profile: ownerProfile }
        : null)

    return resolveActiveContext({
      activeRuntimeSessionId: activeSessionId,
      correlation: activeContextCorrelationFromReceipt(targetSession?.active_context),
      identityV2: identityV2Enabled,
      newChatRoute: displayedDraftRoute,
      owner,
      targetStoredSessionId
    })
  }, [
    activeSessionId,
    draftRoute,
    identityV2Enabled,
    owner,
    ownerConnectionId,
    ownerProfile,
    targetSession,
    targetStoredSessionId
  ])

  const contextAllowsSubmit =
    activeContextCapabilityDecisionResult.known && activeContextCanSubmit(activeContext, identityV2Enabled)

  const submitWithContextGuard = useCallback(
    (text: string, options?: SubmitTextOptions) => (contextAllowsSubmit ? onSubmit(text, options) : false),
    [contextAllowsSubmit, onSubmit]
  )

  const steerWithContextGuard = useCallback(
    (text: string) => (contextAllowsSubmit ? onSteer(text) : false),
    [contextAllowsSubmit, onSteer]
  )

  return { composerDisabled: !gatewayOpen || !contextAllowsSubmit, allowDraftingWhileDisabled: gatewayOpen && !contextAllowsSubmit, activeContext, contextAllowsSubmit, submitWithContextGuard, steerWithContextGuard, identityV2Enabled,
    capabilityState: activeContextCapabilityDecisionResult.known
      ? (identityV2Enabled ? "enabled" : "disabled") : activeContextCapabilityQuery.status }
}
