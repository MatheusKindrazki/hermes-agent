import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveAppIcon, windowIconOptions } from './app-icon'

test('packaged mac icon prefers the Resources fallback outside app.asar', () => {
  const candidates = [
    '/Hermes.app/Contents/Resources/icon.icns',
    '/Hermes.app/Contents/Resources/app.asar/public/apple-touch-icon.png',
  ]

  assert.equal(
    resolveAppIcon(candidates, (candidate) => candidate.endsWith('icon.icns')),
    candidates[0],
  )
})

test('missing packaged icons omit BrowserWindow icon instead of loading a bad path', () => {
  assert.deepEqual(windowIconOptions(undefined), {})
  assert.deepEqual(windowIconOptions('/valid/icon.icns'), { icon: '/valid/icon.icns' })
})
