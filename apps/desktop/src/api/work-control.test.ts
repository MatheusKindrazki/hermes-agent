import { afterEach, describe, expect, it, vi } from 'vitest'

import { getWorkControlProjection } from './work-control'

describe('getWorkControlProjection', () => {
  afterEach(() => {
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('uses only the local read-only GET projection seam', async () => {
    const api = vi.fn().mockResolvedValue({
      schema_version: 'hermes-kernel-projection.v1',
      authority: 'remote',
      generated_at: '2026-09-04T12:00:00Z',
      ttl_seconds: 30,
      records: [],
      stale: false,
      source: 'remote'
    })
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api }
    })

    await expect(getWorkControlProjection()).resolves.toMatchObject({ stale: false, records: [] })
    expect(api).toHaveBeenCalledOnce()
    expect(api).toHaveBeenCalledWith({ path: '/api/work-control/projection' })
  })
})
