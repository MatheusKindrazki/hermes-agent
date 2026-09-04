import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getWorkControlProjection } from '@/api/work-control'

import { WorkControlSection } from './work-control-section'

vi.mock('@/api/work-control', () => ({
  getWorkControlProjection: vi.fn()
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const projection = {
  schema_version: 'hermes-kernel-projection.v1' as const,
  authority: 'remote' as const,
  generated_at: '2026-09-04T12:00:00Z',
  ttl_seconds: 30 as const,
  stale: true,
  source: 'unavailable',
  records: [
    {
      work_id: '01a06c45',
      front: 'Hermes',
      profile: 'kindra',
      status: 'running',
      authority_version: 12,
      updated_at: '2026-09-04T12:00:00Z'
    }
  ]
}

describe('WorkControlSection', () => {
  it('shows only the sanitized read projection and labels stale data', async () => {
    vi.mocked(getWorkControlProjection).mockResolvedValue(projection)

    render(<WorkControlSection />)

    expect(await screen.findByText('Hermes')).toBeTruthy()
    expect(screen.getByText('kindra')).toBeTruthy()
    expect(screen.getByText('running')).toBeTruthy()
    expect(screen.getByText('Stale')).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    expect(document.body.textContent).not.toContain('authority_version')
  })

  it('fails closed when no authoritative snapshot exists', async () => {
    vi.mocked(getWorkControlProjection).mockRejectedValue(new Error('credential leaked here'))

    render(<WorkControlSection />)

    await waitFor(() => expect(screen.getByText('Work Control unavailable')).toBeTruthy())
    expect(document.body.textContent).not.toContain('credential leaked here')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
