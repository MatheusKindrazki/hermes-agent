import { atom } from 'nanostores'

/** Execution signals survive commentary boundaries. Store names, never arguments. */
export const $executingToolsByRuntime = atom<Record<string, Record<string, string>>>({})

export function recordExecutingTool(runtimeId: string, id: string, name: string, running: boolean) {
  const current = $executingToolsByRuntime.get()
  const tools = { ...current[runtimeId] }

  if (running) {
    tools[id] = name
  } else {
    delete tools[id]
  }

  $executingToolsByRuntime.set({ ...current, [runtimeId]: tools })
}

export function clearExecutingTools(runtimeId: string) {
  const current = $executingToolsByRuntime.get()

  if (!(runtimeId in current)) {
    return
  }

  const next = { ...current }
  delete next[runtimeId]
  $executingToolsByRuntime.set(next)
}
