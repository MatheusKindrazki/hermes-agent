import { accessSync, constants, lstatSync, readFileSync, realpathSync, statSync } from 'node:fs'
import path from 'node:path'

import { createSourcePythonBackend, type SourceBackend } from './source-backend'
import { resolveSourcePython } from './source-python'

export const BACKEND_RUNTIME_PIN_FILE = 'backend-runtime.json'

/** Machine-local deployment authority; never read for a remote/SSH connection. */
export function resolvePinnedLocalBackend(options: {
  userData: string
  args: string[]
  env?: NodeJS.ProcessEnv
  bundled?: boolean
}): SourceBackend | null {
  const env = options.env ?? process.env
  const pinPath = path.join(options.userData, BACKEND_RUNTIME_PIN_FILE)
  const override = env.HERMES_DESKTOP_HERMES_ROOT
  // An explicit command override is resolved by the existing command lane.
  if (!override && env.HERMES_DESKTOP_HERMES) return null
  let root: unknown = override
  let python: unknown = override ? env.HERMES_DESKTOP_PYTHON : undefined
  const fail = (reason: string): never => {
    throw new Error(
      `Pinned local Hermes backend is unavailable: ${reason}. Correct ${override ? 'HERMES_DESKTOP_HERMES_ROOT/HERMES_DESKTOP_PYTHON' : pinPath} or remove the pin to choose an installation. No automatic installation was started.`
    )
  }
  if (!override) {
    let stat
    try {
      stat = lstatSync(pinPath)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
      return fail('the pin cannot be read')
    }
    if (options.bundled)
      return fail('this app contains a bundled runtime; use the thin Desktop build for an external runtime pin')
    if (!stat.isFile() || stat.isSymbolicLink()) return fail('the pin must be a regular file')
    if (process.platform !== 'win32' && (stat.uid !== process.getuid?.() || (stat.mode & 0o022) !== 0)) {
      return fail('the pin must be owned by this user and not writable by other users')
    }
    let value: unknown
    try {
      value = JSON.parse(readFileSync(pinPath, 'utf8'))
    } catch {
      return fail('invalid JSON')
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) return fail('expected a versioned runtime object')
    const pin = value as Record<string, unknown>
    if (pin.version !== 1 || Object.keys(pin).some(key => !['version', 'root', 'python'].includes(key))) {
      return fail('unsupported pin schema (expected version 1, root and optional python)')
    }
    root = pin.root
    python = env.HERMES_DESKTOP_PYTHON ?? pin.python
  }
  if (typeof root !== 'string' || !path.isAbsolute(root)) return fail('root must be an absolute source directory')
  if (python !== undefined && (typeof python !== 'string' || !path.isAbsolute(python))) {
    return fail('python must be an absolute executable path')
  }
  try {
    root = realpathSync(root)
    if (!statSync(path.join(root as string, 'hermes_cli', 'main.py')).isFile()) return fail('root has no Hermes CLI')
  } catch {
    return fail('the source directory is missing or unreadable')
  }
  const interpreter = resolveSourcePython(root as string, { override: python as string | undefined })
  if (!interpreter) return fail('the selected runtime has no Python interpreter')
  // An explicit missing Python must not silently fall back to another interpreter.
  if (python !== undefined && interpreter !== python) return fail('the explicit Python executable does not exist')
  try {
    accessSync(interpreter, process.platform === 'win32' ? constants.F_OK : constants.X_OK)
  } catch {
    return fail('Python is not executable')
  }
  const backend = createSourcePythonBackend(root as string, interpreter, options.args, { env })
  if (backend) backend.env.HERMES_INSTALL_ROOT = root as string
  return backend
}
