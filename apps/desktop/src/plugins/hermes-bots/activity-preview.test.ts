import { describe, expect, it } from 'vitest'

import { activityPreview } from './activity-preview'
import type { RosterRow } from './types'

const bot: RosterRow = { name: 'chief', canonical_session: { id: 'root', resolved_id: 'tip' } }

describe('activity previews', () => {
  it('describes real pending operations through the canonical lineage tip', () => {
    expect(activityPreview(bot, 'work', {}, { tip: ['read_file', 'read_file', 'read_file'] })).toBe('Lendo 3 arquivos')
    expect(activityPreview(bot, 'work', {}, { tip: ['terminal'] })).toBe('Executando um comando')
    expect(activityPreview(bot, 'work', {}, { another: ['read_file'] })).toBe('Trabalhando em segundo plano')
  })
  it('prioritizes required input and attention over a busy expression', () => {
    expect(activityPreview(bot, 'work', { tip: 'needs-input' }, {})).toBe('Aguardando sua resposta')
    expect(activityPreview(bot, 'think', {}, {}, true)).toBe('Precisa da sua atenção')
    expect(activityPreview(bot, 'idle', {}, {})).toBeNull()
  })
})
