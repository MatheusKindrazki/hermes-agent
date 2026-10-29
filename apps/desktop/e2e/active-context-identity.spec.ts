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
  waitForAppReady
} from './fixtures'
import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'
import { type ElectronApplication, expect, type Page, test } from './test'

const PROMPT = 'E2E active-context owner receives this prompt.'

function seedProfile(home: string, name: string, mockUrl: string): void {
  const directory = path.join(home, 'profiles', name)

  fs.mkdirSync(directory, { recursive: true })
  writeMockProviderConfig(directory, mockUrl)
  writeEnvFile(directory, 'e2e-mock-key', mockUrl)
}

test.describe('active-context identity', () => {
  let app: ElectronApplication
  let mock: Awaited<ReturnType<typeof startMockServer>>
  let researchMock: Awaited<ReturnType<typeof startMockServer>>
  let page: Page
  let sandbox: Sandbox

  test.beforeAll(async () => {
    test.setTimeout(180_000)
    mock = await startMockServer()
    researchMock = await startMockServer()
    sandbox = createSandbox('active-context')
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome, 'e2e-mock-key', mock.url)
    seedProfile(sandbox.hermesHome, 'research', researchMock.url)
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
    await researchMock?.close()
    sandbox?.cleanup()
  })

  test('shows the draft owner, submits there, and invalidates its route on profile switch', async ({}, testInfo) => {
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

    // The backend may append its first-turn onboarding note to model input.
    // A distinct provider for research proves the chip owner also owns the send.
    await expect.poll(() => researchMock.receivedPrompts.some(prompt => prompt.startsWith(PROMPT)), { timeout: 60_000 }).toBe(true)
    expect(mock.receivedPrompts.some(prompt => prompt.startsWith(PROMPT))).toBe(false)
    await expect(chip).toHaveAttribute('data-active-context-profile', shownOwner.profile ?? '')
    await expect(chip).not.toHaveAttribute('data-active-context-conversation', 'draft')

    const previousConversation = await chip.getAttribute('data-active-context-conversation')
    await testInfo.attach('research-owner-after-send', { body: await page.screenshot(), contentType: 'image/png' })

    const inbox = rail.getByRole('button', { name: 'inbox', exact: true })
    await inbox.click()
    await expect(inbox).toHaveAttribute('aria-pressed', 'true', { timeout: 60_000 })
    await expect(page.locator('[data-slot="statusbar"]').getByText('ready', { exact: true })).toBeVisible({ timeout: 60_000 })
    await expect(chip).toHaveAttribute('data-active-context-conversation', 'draft', { timeout: 60_000 })
    await expect(chip).toHaveAttribute('data-active-context-profile', 'inbox')
    expect(await chip.getAttribute('data-active-context-conversation')).not.toBe(previousConversation)
    await testInfo.attach('inbox-owner-after-switch', { body: await page.screenshot(), contentType: 'image/png' })
    await testInfo.attach('routing-proof', {
      body: JSON.stringify({ shownOwner, previousConversation, switchedProfile: 'inbox',
        researchProviderReceived: researchMock.receivedPrompts.some(prompt => prompt.startsWith(PROMPT)),
        defaultProviderReceived: mock.receivedPrompts.some(prompt => prompt.startsWith(PROMPT)) }),
      contentType: 'application/json'
    })
  })
})
