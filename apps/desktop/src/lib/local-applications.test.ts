import { describe, expect, it } from 'vitest'

import { localApplicationCompletions, localApplicationContext } from './local-applications'
const catalog = {
  machine: 'portable.local',
  apps: [{ name: 'Slack', bundleId: 'com.tinyspeck.slackmacgap', path: '/Applications/Slack.app' }]
}
describe('local application references', () => {
  it('matches the human app name and carries the client machine', () => {
    expect(localApplicationCompletions(catalog, 'slack')).toEqual([
      {
        insert: '@app/com.tinyspeck.slackmacgap',
        display: 'Slack',
        meta: 'Aplicativo · portable.local',
        icon: 'device-desktop'
      }
    ])
    expect(localApplicationCompletions(catalog, 'missing')).toEqual([])
  })
  it('resolves only discovered identities and preserves the original request', () => {
    const text = 'Veja @app/com.tinyspeck.slackmacgap agora'
    const result = localApplicationContext(text, catalog)
    expect(result.startsWith(text)).toBe(true)
    expect(result).toContain('portable.local')
    expect(result).toContain('not automatically to the agent backend')
    expect(localApplicationContext('@app/com.fake.app', catalog)).toBe('@app/com.fake.app')
    expect(localApplicationContext('email@app/com.tinyspeck.slackmacgap', catalog)).toBe(
      'email@app/com.tinyspeck.slackmacgap'
    )
  })
})
