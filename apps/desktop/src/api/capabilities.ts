import { capabilityScoped, type ProfileScope } from './client'

export interface ApiCapabilitiesResponse {
  features?: {
    active_context_v2?: {
      enabled?: boolean
      receipt_schema?: string
    }
  }
}

/** Read rollout capabilities from the exact backend/profile being targeted. */
export function getApiCapabilities(scope?: ProfileScope): Promise<ApiCapabilitiesResponse> {
  return window.hermesDesktop.api<ApiCapabilitiesResponse>({
    ...capabilityScoped(scope),
    path: '/v1/capabilities'
  })
}
