import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  $executorDrafts,
  ExecutorComposer,
  changeExecutorDraft,
  draftKey,
  submitExecutorDraft
} from './parallel-executor-compose'
const url = 'https://mac-mini-de-matheus.tail0673c.ts.net:8520/conversations/412a934d-ca47-4fc8-a100-713ab88c5882'
const other = url.replace('412a934d', '512a934d')
beforeEach(() => {
  $executorDrafts.set({})
})
afterEach(cleanup)
it('only sends on explicit submit, clears after confirmation and requests a fresh transcript', async () => {
  const send = vi.fn().mockResolvedValue({ ok: true, message: 'Mensagem salva. Aguardando vaga.' }),
    refresh = vi.fn()
  window.hermesDesktop = { ...window.hermesDesktop, sendParallelExecutor: send }
  render(<ExecutorComposer url={url} recipient="Engenharia da tarefa" onSent={refresh} />)
  expect(screen.getByText('Para: Engenharia da tarefa')).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Priorize o teste' } })
  expect(send).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Enviar ao agente' }))
  await screen.findByText('Mensagem salva. Aguardando vaga.')
  expect(send).toHaveBeenCalledWith(url, 'Priorize o teste')
  expect(send).toHaveBeenCalledTimes(1)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(refresh).toHaveBeenCalledTimes(1)
})
it('preserves scoped drafts on closing and switching conversations', () => {
  const first = render(<ExecutorComposer url={url} recipient="A" onSent={() => {}} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Só para A' } })
  first.unmount()
  const second = render(<ExecutorComposer url={other} recipient="B" onSent={() => {}} />)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  second.unmount()
  render(<ExecutorComposer url={url + '?backend=existing'} recipient="A" onSent={() => {}} />)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Só para A')
})
it('guards double submit and keeps an unconfirmed message without automatic retries', async () => {
  let finish!: (value: unknown) => void
  const send = vi.fn(
    () =>
      new Promise<any>(resolve => {
        finish = resolve
      })
  )
  window.hermesDesktop = { ...window.hermesDesktop, sendParallelExecutor: send }
  changeExecutorDraft(url, 'Corrigir A')
  changeExecutorDraft(other, 'Corrigir B')
  const pending = submitExecutorDraft(url)
  await submitExecutorDraft(url)
  expect(send).toHaveBeenCalledTimes(1)
  changeExecutorDraft(url, 'should not replace pending')
  finish({ ok: false, message: 'Não foi possível confirmar o envio.' })
  await pending
  expect($executorDrafts.get()[draftKey(url)].text).toBe('Corrigir A')
  expect($executorDrafts.get()[draftKey(other)].text).toBe('Corrigir B')
  expect($executorDrafts.get()[draftKey(url)].pending).toBe(false)
  expect(send).toHaveBeenCalledTimes(1)
})
