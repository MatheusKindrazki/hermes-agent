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
  it('names explicit operations without inventing what a shell command does', () => {
    expect(activityPreview(bot, 'work', {}, { tip: ['apply_patch'] })).toBe('Editando um arquivo')
    expect(activityPreview(bot, 'work', {}, { tip: ['mcp__cua__click'] })).toBe('Operando um aplicativo')
    expect(activityPreview(bot, 'work', {}, { tip: ['run_tests'] })).toBe('Rodando testes')
    expect(activityPreview(bot, 'work', {}, { tip: ['terminal'] })).toBe('Executando um comando')
    expect(activityPreview(bot, 'work', {}, { tip: ['read_file', 'terminal'] })).toBe('Executando 2 ações')
    expect(activityPreview(bot, 'think', { tip: 'stalled' }, {})).toBe('Sem progresso recente')
  })
})
