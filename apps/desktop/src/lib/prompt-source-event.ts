/** Allocate once per logical input, outside every transport retry closure. */
export function createPromptSourceEventId(): string {
  return crypto.randomUUID()
}

export function isPromptSourceEventId(value: unknown): value is string {
  return (
    typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)
  )
}
