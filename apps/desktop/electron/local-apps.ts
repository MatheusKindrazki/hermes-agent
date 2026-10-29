import { execFile } from 'node:child_process'
import { readdir } from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'

const exec = promisify(execFile)
export interface LocalApplication {
  name: string
  bundleId: string
  path: string
}

/** Read application identities only. Never launch an app or request OS access. */
export async function discoverLocalApplications(roots: string[], extraApp?: string): Promise<LocalApplication[]> {
  const candidates = new Set<string>()

  async function walk(root: string, depth: number) {
    const entries = await readdir(root, { withFileTypes: true }).catch(() => [])

    for (const entry of entries) {
      if (!entry.isDirectory()) {
        continue
      }
      const full = path.join(root, entry.name)

      if (entry.name.endsWith('.app')) {
        candidates.add(full)
      } else if (depth > 0) {
        await walk(full, depth - 1)
      }
    }
  }

  await Promise.all(roots.map(root => walk(root, 1)))

  if (extraApp?.endsWith('.app')) {
    candidates.add(extraApp)
  }
  const items: LocalApplication[] = []
  const queue = Array.from(candidates).slice(0, 512)
  await Promise.all(
    Array.from({ length: 8 }, async () => {
      while (queue.length) {
        const appPath = queue.pop()!

        try {
          const { stdout } = await exec(
            '/usr/bin/plutil',
            ['-extract', 'CFBundleIdentifier', 'raw', '-o', '-', path.join(appPath, 'Contents/Info.plist')],
            { timeout: 1500, maxBuffer: 4096 }
          )

          const bundleId = stdout.trim()

          if (/^[a-zA-Z0-9][a-zA-Z0-9._-]+$/.test(bundleId)) {
            items.push({ name: path.basename(appPath, '.app'), bundleId, path: appPath })
          }
        } catch {
          /* Unreadable bundles are not usable references. */
        }
      }
    })
  )
  const unique = new Map<string, LocalApplication>()

  for (const item of items.sort((a, b) => a.path.localeCompare(b.path))) {
    if (!unique.has(item.bundleId)) {
      unique.set(item.bundleId, item)
    }
  }

  return Array.from(unique.values()).sort((a, b) => a.name.localeCompare(b.name))
}
