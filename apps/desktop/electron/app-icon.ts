export function resolveAppIcon(
  candidates: readonly string[],
  exists: (candidate: string) => boolean,
): string | undefined {
  return candidates.find((candidate) => {
    try {
      return Boolean(candidate) && exists(candidate)
    } catch {
      return false
    }
  })
}

export function windowIconOptions(icon: string | undefined): { icon?: string } {
  return icon ? { icon } : {}
}
