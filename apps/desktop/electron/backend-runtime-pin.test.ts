import assert from 'node:assert/strict'
import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, test } from 'vitest'
import { resolvePinnedLocalBackend } from './backend-runtime-pin'
import { runPrimaryBackendStartup } from './primary-backend-startup'

const temporary: string[] = []
afterEach(() => {
  for (const dir of temporary.splice(0)) rmSync(dir, { recursive: true, force: true })
})
function fixture() {
  const dir = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'hermes-runtime-pin-')))
  temporary.push(dir)
  const root = path.join(dir, 'release')
  const python = process.platform === 'win32'
    ? path.join(root, 'venv', 'Scripts', 'python.exe')
    : path.join(root, 'venv', 'bin', 'python')
  mkdirSync(path.join(root, 'hermes_cli'), { recursive: true })
  mkdirSync(path.dirname(python), { recursive: true })
  writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '# source fixture\n')
  writeFileSync(python, '#!/bin/sh\nexit 0\n', { mode: 0o700 })
  const file = path.join(dir, 'backend-runtime.json')
  const pin = (value: unknown) => writeFileSync(file, JSON.stringify(value), { mode: 0o600 })
  return { dir, root, python, file, pin }
}
test('Finder launch without environment uses the persisted release and its own Python', () => {
  const f = fixture()
  f.pin({ version: 1, root: f.root })
  const backend = resolvePinnedLocalBackend({ userData: f.dir, args: ['serve'], env: {} })
  assert.equal(backend?.root, f.root)
  assert.equal(backend.command, f.python)
  assert.deepEqual(backend.args, ['-m', 'hermes_cli.main', 'serve'])
  assert.equal(backend.env.PYTHONPATH, f.root)
  assert.equal(backend.env.HERMES_INSTALL_ROOT, f.root)
  assert.equal(backend.bootstrap, false)
})
test('explicit environment root wins over a malformed persisted pin', () => {
  const f = fixture()
  f.pin({ version: 99 })
  const backend = resolvePinnedLocalBackend({
    userData: f.dir,
    args: [],
    env: { HERMES_DESKTOP_HERMES_ROOT: f.root, HERMES_DESKTOP_PYTHON: f.python }
  })
  assert.equal(backend?.root, f.root)
})
test('missing explicit Python refuses rather than substituting the release venv', () => {
  const f = fixture()
  f.pin({ version: 1, root: f.root, python: path.join(f.root, 'missing') })
  assert.throws(
    () => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }),
    /explicit Python.*No automatic installation/
  )
})
test('invalid root/schema and non-executable Python fail closed with a remedy', () => {
  const f = fixture()
  for (const value of [
    { version: 1, root: 'relative' },
    { version: 2, root: f.root },
    { version: 1, root: '/missing-hermes-release' }
  ]) {
    f.pin(value)
    assert.throws(
      () => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }),
      /Correct .*backend-runtime.json/
    )
  }
  if (process.platform !== 'win32') {
    f.pin({ version: 1, root: f.root })
    chmodSync(f.python, 0o600)
    assert.throws(() => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }), /not executable/)
  }
})
test('an external pin cannot silently disappear behind a bundled runtime', () => {
  const f = fixture()
  f.pin({ version: 1, root: f.root })
  assert.throws(
    () => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {}, bundled: true }),
    /thin Desktop build/
  )
})
test('absent pin leaves normal discovery unchanged; explicit CLI also owns resolution', () => {
  const f = fixture()
  assert.equal(resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }), null)
  f.pin({ version: 99 })
  assert.equal(
    resolvePinnedLocalBackend({ userData: f.dir, args: [], env: { HERMES_DESKTOP_HERMES: '/explicit/hermes' } }),
    null
  )
})

test.each(['remote', 'ssh'])('saved %s startup never invokes the local pinned runtime resolver', async kind => {
  const f = fixture()
  f.pin({ version: 99 })
  const result = await runPrimaryBackendStartup({
    assertCurrentAttempt: () => {},
    connectRemote: async (remote: { kind: string }) => remote,
    ensureLocalRuntime: async backend => backend,
    prepareLocalBackend: () => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }),
    resolveRemote: async () => ({ kind }),
    waitForDecision: async () => 'continue-local' as const,
    waitForLocalStart: async () => {}
  })
  assert.equal(result.kind, 'remote')
})

test('Python-only environment override wins over the persisted interpreter', () => {
  const f = fixture()
  f.pin({ version: 1, root: f.root, python: path.join(f.root, 'retired-python') })
  const backend = resolvePinnedLocalBackend({ userData: f.dir, args: [], env: { HERMES_DESKTOP_PYTHON: f.python } })
  assert.equal(backend?.command, f.python)
})

test.skipIf(process.platform === 'win32')('shared-writable and symlink pins are refused', () => {
  const f = fixture()
  f.pin({ version: 1, root: f.root })
  chmodSync(f.file, 0o666)
  assert.throws(() => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }), /not writable by other users/)
  rmSync(f.file)
  const target = path.join(f.dir, 'other.json')
  writeFileSync(target, JSON.stringify({ version: 1, root: f.root }), { mode: 0o600 })
  symlinkSync(target, f.file)
  assert.throws(() => resolvePinnedLocalBackend({ userData: f.dir, args: [], env: {} }), /regular file/)
})
