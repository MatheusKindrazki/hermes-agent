/**
 * The draft identity painted in the titlebar is the route that owns Enter.
 * This exercises the complete renderer -> gateway -> mock-provider path and
 * then switches profile to prove the prior conversation route is discarded.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  type MockBackendFixture,
  type Sandbox,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { startMockServer } from './mock-server'
import { type ElectronApplication, expect, type Page, test } from './test'

const PROMPT = 'E2E active-context owner receives this prompt.'

function seedProfile(home: string, name: string, mockUrl: string): void {
  const directory = path.join(home, 'profiles', name)

  fs.mkdirSync(directory, { recursive: true })
  writeMockProviderConfig(directory, mockUrl)
  writeEnvFile(directory)
}

test.describe('active-context identity', () => {
  let app: ElectronApplication
  let mock: Awaited<ReturnType<typeof startMockServer>>
  let page: Page
  let sandbox: Sandbox

  test.beforeAll(async () => {
    test.setTimeout(180_000)
    mock = await startMockServer()
    sandbox = createSandbox('active-context')
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome)
    seedProfile(sandbox.hermesHome, 'research', mock.url)
    seedProfile(sandbox.hermesHome, 'inbox', mock.url)

    ;({ app, page } = await launchDesktop(buildAppEnv(sandbox)))
    await waitForAppReady({ app, page } as MockBackendFixture, 120_000)
    await expect(page.locator('[data-slot="statusbar"]').getByText('ready', { exact: true })).toBeVisible({
      timeout: 120_000
    })
  })

  test.afterAll(async () => {
    await app?.close().catch(() => undefined)
    await mock?.close()
    sandbox?.cleanup()
  })

  test('shows the draft owner, submits there, and invalidates its route on profile switch', async () => {
    test.setTimeout(180_000)
    const rail = page.locator('[data-slot="profile-rail"]')

    const research = rail.getByRole('button', { name: 'research', exact: true })

    await expect(research).toBeVisible({ timeout: 60_000 })
    await research.click()
    await expect(research).toHaveAttribute('aria-pressed', 'true', { timeout: 120_000 })
    await expect(page.locator('[data-slot="statusbar"]').getByText('ready', { exact: true })).toBeVisible({
      timeout: 120_000
    })

    const chip = page.locator('[data-slot="active-context-chip"]')

    await expect(chip).toBeVisible({ timeout: 60_000 })
    await expect(chip).not.toHaveText('')
    await expect(chip).toHaveAttribute('data-active-context-conversation', 'draft')
    await expect(chip).toHaveAttribute('data-active-context-profile', 'research')
    await expect(chip).toHaveAttribute('data-active-context-tenant', /.+/)
    await expect(chip).toHaveAttribute('data-active-context-machine', /.+/)

    const shownOwner = await chip.evaluate(element => ({
      connection: element.getAttribute('data-active-context-connection'),
      profile: element.getAttribute('data-active-context-profile')
    }))

    expect(shownOwner).toEqual({ connection: 'local', profile: 'research' })

    const composer = page.locator('[contenteditable="true"]').first()

    await composer.click()
    await composer.fill(PROMPT)
    await page.keyboard.press('Enter')

    await expect.poll(() => mock.receivedPrompts.includes(PROMPT), { timeout: 60_000 }).toBe(true)
    await expect(chip).toHaveAttribute('data-active-context-profile', shownOwner.profile ?? '')
    await expect(chip).not.toHaveAttribute('data-active-context-conversation', 'draft')

    const previousConversation = await chip.getAttribute('data-active-context-conversation')

    await rail.getByRole('button', { name: 'inbox', exact: true }).click()
    await expect(chip).toHaveAttribute('data-active-context-conversation', 'draft', { timeout: 60_000 })
    await expect(chip).toHaveAttribute('data-active-context-profile', 'inbox')
    expect(await chip.getAttribute('data-active-context-conversation')).not.toBe(previousConversation)
  })
})
