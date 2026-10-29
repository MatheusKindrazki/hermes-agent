import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useId } from 'react'

type Draft = { text: string; pending: boolean; receipt?: { ok: boolean; message: string } }
const empty: Draft = { text: '', pending: false }
// Window-local drafts survive closing the panel, keyed by conversation, not by the selected character.
export const $executorDrafts = atom<Record<string, Draft>>({})
export function draftKey(url: string) {
  const ref = new URL(url)
  return ref.origin + ref.pathname.replace(/\/$/, '')
}
export function changeExecutorDraft(url: string, text: string) {
  const key = draftKey(url),
    current = $executorDrafts.get()[key] || empty
  if (current.pending) return
  $executorDrafts.set({ ...$executorDrafts.get(), [key]: { ...current, text, receipt: undefined } })
}
export async function submitExecutorDraft(url: string) {
  const key = draftKey(url),
    draft = $executorDrafts.get()[key] || empty
  if (draft.pending || !draft.text.trim() || draft.text.length > 20000 || !window.hermesDesktop?.sendParallelExecutor)
    return false
  $executorDrafts.set({ ...$executorDrafts.get(), [key]: { ...draft, pending: true, receipt: undefined } })
  let receipt: { ok: boolean; message: string }
  try {
    receipt = await window.hermesDesktop.sendParallelExecutor(url, draft.text)
  } catch {
    receipt = {
      ok: false,
      message: 'Não foi possível confirmar o envio. Confira as mensagens antes de tentar novamente.'
    }
  }
  $executorDrafts.set({
    ...$executorDrafts.get(),
    [key]: { text: receipt.ok ? '' : draft.text, pending: false, receipt }
  })
  return receipt.ok
}
export function ExecutorComposer({ url, recipient, onSent }: { url: string; recipient: string; onSent: () => void }) {
  const drafts = useStore($executorDrafts),
    draft = drafts[draftKey(url)] || empty,
    id = useId()
  return (
    <form
      className="sticky bottom-0 -mx-1 border-t border-white/8 bg-[#171719] px-1 pt-3 pb-2"
      onSubmit={async event => {
        event.preventDefault()
        if (await submitExecutorDraft(url)) onSent()
      }}
    >
      <label htmlFor={id} className="mb-2 block truncate text-[11px] text-white/45" title={recipient}>
        Para: {recipient}
      </label>
      <textarea
        id={id}
        aria-label="Mensagem para o agente"
        rows={2}
        maxLength={20000}
        value={draft.text}
        disabled={draft.pending}
        placeholder="Ajustar o rumo, dar contexto…"
        onChange={event => changeExecutorDraft(url, event.target.value)}
        className="block max-h-40 min-h-16 w-full resize-y rounded-lg border border-white/10 bg-white/[0.035] px-3 py-2 text-[13px] leading-relaxed text-white/85 placeholder:text-white/30 focus:border-white/30 focus:outline-none disabled:opacity-60"
      />
      <div className="mt-2 flex items-start justify-between gap-3">
        <p
          role="status"
          className={
            'min-w-0 flex-1 text-[11px] leading-relaxed ' +
            (draft.receipt?.ok === false ? 'text-amber-200/70' : 'text-white/45')
          }
        >
          {draft.receipt?.message || (draft.pending ? 'Enviando…' : 'A mensagem entra nesta conversa do agente.')}
        </p>
        <button
          type="submit"
          disabled={draft.pending || !draft.text.trim() || !window.hermesDesktop?.sendParallelExecutor}
          className="shrink-0 rounded-md bg-white/10 px-3 py-1.5 text-xs text-white/85 hover:bg-white/15 disabled:opacity-35"
        >
          {draft.pending ? 'Enviando…' : 'Enviar ao agente'}
        </button>
      </div>
    </form>
  )
}
