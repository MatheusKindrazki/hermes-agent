import { hermesApi } from './client'

export interface WorkControlRecord {
  work_id: string
  front: string
  profile: string | null
  status: string
  authority_version: number
  updated_at: string
}

export interface WorkControlProjection {
  schema_version: 'hermes-kernel-projection.v1'
  authority: 'remote'
  generated_at: string
  ttl_seconds: 30
  records: WorkControlRecord[]
  stale: boolean
  source: string
  error?: string
}

/** Read-only local backend seam. Jarvis credentials never enter the renderer. */
export function getWorkControlProjection(): Promise<WorkControlProjection> {
  return hermesApi<WorkControlProjection>({ path: '/api/work-control/projection' })
}
