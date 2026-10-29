import { atom } from 'nanostores'

export interface LocalApplication {
  name: string
  bundleId: string
  path: string
}
export interface LocalApplicationCatalog {
  machine: string
  apps: LocalApplication[]
}
export const $localApplications = atom<LocalApplicationCatalog | null>(null)
let pending: Promise<LocalApplicationCatalog | null> | null = null

export function loadLocalApplications(): Promise<LocalApplicationCatalog | null> {
  if (!window.hermesDesktop?.listLocalApplications) {
    return Promise.resolve(null)
  }

  pending ??= window.hermesDesktop
    .listLocalApplications()
    .then(value => {
      if (typeof value.machine !== 'string' || !Array.isArray(value.apps)) {
        return null
      }

      const catalog = {
        machine: value.machine,
        apps: value.apps.filter(
          app =>
            typeof app.name === 'string' &&
            typeof app.path === 'string' &&
            /^[a-zA-Z0-9][a-zA-Z0-9._-]+$/.test(app.bundleId)
        )
      }

      $localApplications.set(catalog)

      return catalog
    })
    .catch(() => {
      pending = null

      return null
    })

  return pending
}

export function localApplicationCompletions(catalog: LocalApplicationCatalog | null, query: string) {
  const q = query.toLocaleLowerCase().replace(/^app\//, '')

  return (catalog?.apps ?? [])
    .filter(app => !q || app.name.toLocaleLowerCase().includes(q) || app.bundleId.toLocaleLowerCase().includes(q))
    .slice(0, q ? 24 : 8)
    .map(app => ({
      insert: `@app/${app.bundleId}`,
      display: app.name,
      meta: `Aplicativo · ${catalog!.machine}`,
      icon: 'device-desktop'
    }))
}

/** Only resolve identities returned by local discovery, never a guessed bundle or path. */
export function localApplicationContext(text: string, catalog: LocalApplicationCatalog | null): string {
  if (!catalog) {
    return text
  }

  const ids = new Set(Array.from(text.matchAll(/(?:^|\s)@app\/([a-zA-Z0-9._-]+)(?=\s|$)/g), match => match[1]))
  const apps = catalog.apps.filter(app => ids.has(app.bundleId))

  if (!apps.length) {
    return text
  }

  return `${text}\n\n[Desktop application references: ${JSON.stringify({ machine: catalog.machine, scope: 'desktop-client', applications: apps })}. These applications belong to the user's desktop client, not automatically to the agent backend. Use an existing authorized computer-use connection to this exact machine; do not substitute another host or claim access from this reference alone.]`
}
