import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { discoverLocalApplications } from './local-apps'
describe.skipIf(process.platform !== 'darwin')('local application discovery', () => {
  it('reads real plist identities, ignores invalid bundles and deduplicates installed copies', async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'kindra-apps-'))

    try {
      for (const name of ['Slack', 'Duplicate', 'Broken']) {
        await mkdir(path.join(root, `${name}.app/Contents`), { recursive: true })

        if (name !== 'Broken') {
          await writeFile(
            path.join(root, `${name}.app/Contents/Info.plist`),
            '<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.example.slack</string></dict></plist>'
          )
        }
      }

      const apps = await discoverLocalApplications([root, path.join(root, 'missing')])
      expect(apps).toHaveLength(1)
      expect(apps[0].bundleId).toBe('com.example.slack')
      expect(apps[0].path.startsWith(root)).toBe(true)
      expect(await discoverLocalApplications([path.join(root, 'missing')])).toEqual([])
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })
})
