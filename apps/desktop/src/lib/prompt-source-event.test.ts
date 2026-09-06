import { expect, it } from 'vitest'
import { createPromptSourceEventId, isPromptSourceEventId } from './prompt-source-event'

it('allocates distinct input identities independently of text', () => {
  const first = createPromptSourceEventId()
  const second = createPromptSourceEventId()
  expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  expect(second).not.toBe(first)
})

it.each([
  undefined,
  null,
  '',
  'queued-legacy',
  '11111111-1111-7111-8111-111111111111',
  'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'
])('does not accept malformed or noncanonical carrier %s', value => {
  expect(isPromptSourceEventId(value)).toBe(false)
})

it('accepts the exact UUID4 wire carrier', () => {
  expect(isPromptSourceEventId('11111111-1111-4111-8111-111111111111')).toBe(true)
})
