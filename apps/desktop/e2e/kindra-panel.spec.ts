import fs from 'node:fs'
import path from 'node:path'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import { buildAppEnv, createSandbox, launchDesktop, type MockBackendFixture, waitForAppReady } from './fixtures'
import { RealSessionBuilder } from './real-session-builder'
import { expect, test } from './test'

const origin = 'https://mac-mini-de-matheus.tail0673c.ts.net:8525'
const id = '11111111-2222-4333-8444-555555555555'

// Actual renderer and isolated gateway; the panel and executor content are fixtures.
// Never read or write a live executor conversation in this automated test.
test('opens the embedded panel and its conversation without losing the principal chat', async () => {
  test.setTimeout(180_000)
  const sandbox = createSandbox('kindra-panel')
  const mock = await startMockServer()
  let app: MockBackendFixture['app'] | undefined
  try {
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome, 'e2e-key', mock.url)
    const profile = path.join(sandbox.hermesHome, 'profiles', 'alpha')
    fs.mkdirSync(profile, { recursive: true })
    writeMockProviderConfig(profile, mock.url)
    writeEnvFile(profile, 'e2e-key', mock.url)
    const builder = await RealSessionBuilder.start(profile)
    try {
      await builder.createSession({ title: 'Bot Chat', turns: ['Hello alpha'] })
    } finally {
      await builder.close()
    }
    const launched = await launchDesktop(buildAppEnv(sandbox))
    app = launched.app
    const { page } = launched
    await app.evaluate(({ ipcMain }, conversationId) => {
      ipcMain.removeHandler('hermes:parallel:read')
      ipcMain.handle('hermes:parallel:read', (_event, url: string) => {
        if (!url.endsWith('/conversations/' + conversationId)) {
          throw new Error('Unexpected fixture conversation')
        }
        return { kind: 'conversation', id: conversationId, title: 'Engenharia · teste', status: 'RUNNING', items: [{ role: 'agent', text: 'Conversa integrada em tempo real.', at: new Date().toISOString() }], fetchedAt: new Date().toISOString() }
      })
      ipcMain.removeHandler('hermes:parallel:send')
      ipcMain.handle('hermes:parallel:send', () => { throw new Error('This read-only test must never send') })
    }, id)
    await page.route(origin + '/painel/**', route => route.fulfill({ contentType: 'text/html', body: `<html><body style="background:#171719;color:white"><h1>Plataforma de teste</h1><button onclick="parent.postMessage({type:'kd-route',hash:'#/conversa/${id}'},'*')">Abrir conversa</button><script>parent.postMessage({type:'kd-hello'},'*')</script></body></html>` }))
    await waitForAppReady({ app, page } as MockBackendFixture, 120_000)
    await page.getByRole('button', { name: 'Bots', exact: true }).or(page.getByRole('tab', { name: 'Bots', exact: true })).first().click()
    await page.getByRole('button', { name: 'Todos', exact: true }).click()
    const row = page.locator('[data-slot="bots-roster"] [data-roster-key="local::alpha"]')
    await expect(row).toBeVisible({ timeout: 30000 })
    await row.click()
    await expect(page.getByRole('button', { name: 'Painel', exact: true })).toBeVisible({ timeout: 60000 })
    const principal = page.url()
    await page.getByRole('button', { name: 'Painel', exact: true }).click()
    const panel = page.frameLocator('iframe[title="Kindra Painel"]')
    await expect(panel.getByRole('heading', { name: 'Plataforma de teste' })).toBeVisible()
    expect(await panel.locator('body').evaluate(() => 'hermesDesktop' in window)).toBe(false)
    await panel.getByRole('button', { name: 'Abrir conversa' }).click()
    await expect(page.getByRole('heading', { name: 'Em paralelo', exact: true })).toBeVisible()
    await expect(page.locator('[data-slot="sheet-content"]')).toHaveCSS('background-color', 'rgb(23, 23, 25)')
    await expect(page.getByText('Conversa integrada em tempo real.')).toBeVisible()
    await expect(page.getByText('Para: Engenharia · teste')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Enviar ao agente' })).toBeDisabled()
    expect(page.url()).toBe(principal)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('heading', { name: 'Em paralelo', exact: true })).not.toBeVisible()
    await expect(panel.getByRole('heading', { name: 'Plataforma de teste' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: 'Painel', exact: true })).toBeVisible()
    expect(page.url()).toBe(principal)
  } finally {
    await app?.close().catch(() => undefined)
    await mock.close()
    sandbox.cleanup()
  }
})
