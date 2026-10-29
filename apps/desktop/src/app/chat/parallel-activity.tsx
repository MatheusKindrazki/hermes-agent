import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { useSessionSlice } from '@/lib/use-session-slice'
import { knownOwnerForSession, requestForOwnedSession } from '@/store/session-states'
import { $subagentsBySession, reconcileSubagentSnapshot, type SubagentPayload } from '@/store/subagents'

import { SubagentTranscript } from './composer/status-stack/subagent-transcript'
import { rejectUnownedSubagentRequest } from './composer/status-stack/use-subagent-snapshot'
import { parallelLinks } from './parallel-links'
import { agentExchanges } from './parallel-exchanges'
import { useSessionView } from './session-view'

const statuses = {
  running: 'Em andamento',
  queued: 'Na fila',
  completed: 'Concluído',
  failed: 'Falhou',
  interrupted: 'Interrompido'
}

function ParallelContent({ sessionId }: { sessionId: string | null }) {
  const view = useSessionView()
  const messages = useStore(view.$messages)
  const links = useMemo(() => parallelLinks(messages), [messages])
  const exchanges = useMemo(() => agentExchanges(messages), [messages])
  const agents = useSessionSlice($subagentsBySession, sessionId)
  const visible = usePaneVisible()
  const [load, setLoad] = useState<'loading' | 'ready' | 'error'>('loading')
  const [selected, setSelected] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    if (!sessionId || !visible) return
    let cancelled = false
    let pending = false
    let failures = 0
    const refresh = async () => {
      if (cancelled || pending || failures >= 3 || document.visibilityState === 'hidden') return
      pending = true
      const before = $subagentsBySession.get()[sessionId]
      const owner = JSON.stringify(knownOwnerForSession(sessionId))
      try {
        const snapshot = await requestForOwnedSession<{
          subagents: SubagentPayload[]
          delegations?: SubagentPayload[]
        }>(sessionId, rejectUnownedSubagentRequest, 'subagent.list', { session_id: sessionId })
        if (!Array.isArray(snapshot.subagents)) throw new Error('Invalid agent snapshot')
        if (!cancelled && owner === JSON.stringify(knownOwnerForSession(sessionId))) {
          if (before === $subagentsBySession.get()[sessionId]) {
            reconcileSubagentSnapshot(sessionId, snapshot.subagents, snapshot.delegations ?? [])
          }
          setLoad('ready')
          failures = 0
        }
      } catch {
        failures++
        if (!cancelled) setLoad('error')
      } finally {
        pending = false
      }
    }
    setLoad('loading')
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [sessionId, visible, retry])

  return (
    <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 pb-6 text-sm">
      <section aria-label="Agentes desta conversa" className="space-y-3">
        <h3 className="font-medium">Agentes desta conversa</h3>
        {!sessionId ? (
          <p className="text-muted-foreground">Abra a conversa para consultar os agentes.</p>
        ) : load === 'loading' ? (
          <p role="status" className="text-muted-foreground">
            Consultando atividade…
          </p>
        ) : load === 'error' ? (
          <div role="status" className="space-y-2 text-muted-foreground">
            <p>Não foi possível atualizar os agentes. Os registros abaixo podem estar desatualizados.</p>
            <Button variant="ghost" size="sm" onClick={() => setRetry(value => value + 1)}>
              Tentar novamente
            </Button>
          </div>
        ) : agents.length === 0 ? (
          <p className="text-muted-foreground">Nenhum subagente informado nesta conversa.</p>
        ) : null}
        {agents.map(agent => (
          <div key={agent.id} className="rounded-lg border border-(--ui-stroke-tertiary)">
            <button
              type="button"
              aria-expanded={selected === agent.id}
              className="w-full space-y-1 p-3 text-left hover:bg-(--chrome-action-hover)"
              onClick={() => setSelected(selected === agent.id ? null : agent.id)}
            >
              <span className="block text-xs text-muted-foreground">{statuses[agent.status]}</span>
              <span className="block font-medium">{agent.goal || 'Tarefa do agente'}</span>
              <span className="block truncate text-xs text-muted-foreground">
                {agent.summary || agent.stream.at(-1)?.text || 'Aguardando mensagens'}
              </span>
              <span className="block text-xs text-(--ui-text-secondary)">
                {selected === agent.id ? 'Recolher conversa' : 'Ver conversa'}
              </span>
            </button>
            {selected === agent.id && sessionId && (
              <div className="border-t border-(--ui-stroke-tertiary) px-3 pb-3">
                {agent.stream.length > 0 && (
                  <div className="my-3 space-y-2 text-xs">
                    {agent.stream.map((entry, index) => (
                      <p key={index} className="whitespace-pre-wrap break-words">
                        {entry.text}
                      </p>
                    ))}
                  </div>
                )}
                <SubagentTranscript key={sessionId + ':' + agent.id} sessionId={sessionId} subagentId={agent.id} />
              </div>
            )}
          </div>
        ))}
      </section>
      <section aria-label="Trocas entre personagens" className="space-y-3">
        <h3 className="font-medium">Trocas entre personagens</h3>
        <p className="text-xs text-muted-foreground">
          Mensagens registradas no trecho carregado, das mais recentes para as anteriores.
        </p>
        {exchanges.length === 0 ? (
          <p className="text-muted-foreground">Nenhuma troca entre personagens neste trecho.</p>
        ) : (
          exchanges.map(exchange => (
            <details key={exchange.id} className="rounded-lg border border-(--ui-stroke-tertiary) p-3">
              <summary className="cursor-pointer space-y-1">
                <span className="font-medium">{exchange.label}</span>
                <span className="block truncate text-xs text-muted-foreground">{exchange.text.slice(0, 140)}</span>
              </summary>
              <p className="mt-3 whitespace-pre-wrap break-words text-xs leading-relaxed">{exchange.text}</p>
            </details>
          ))
        )}
      </section>
      <section aria-label="Entregas externas" className="space-y-3">
        <h3 className="font-medium">Entregas externas</h3>
        <p className="text-xs text-muted-foreground">
          Links recebidos no trecho carregado desta conversa. Abra para ver o andamento e as mensagens no executor.
        </p>
        {links.length === 0 ? (
          <p className="text-muted-foreground">Nenhum link de acompanhamento neste trecho.</p>
        ) : (
          links.map(link => (
            <a
              key={link.url}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="block space-y-1 rounded-lg border border-(--ui-stroke-tertiary) p-3 hover:bg-(--chrome-action-hover)"
            >
              <span className="block font-medium">{link.title}</span>
              <span className="block text-xs text-muted-foreground">
                {link.kind === 'task' ? 'Acompanhar tarefa ↗' : 'Abrir conversa dos agentes ↗'}
              </span>
            </a>
          ))
        )}
      </section>
    </div>
  )
}

export function ParallelActivity() {
  const view = useSessionView()
  const sessionId = useStore(view.$runtimeId)
  const [open, setOpen] = useState(false)
  useEffect(() => setOpen(false), [sessionId])
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="sm" className="shrink-0 text-xs text-muted-foreground">
          Em paralelo
        </Button>
      </SheetTrigger>
      <SheetContent className="w-[min(92vw,34rem)] sm:max-w-[34rem]">
        <SheetHeader className="px-5 pt-6 pr-12">
          <SheetTitle>Em paralelo</SheetTitle>
          <SheetDescription>Tarefas e conversas dos agentes, sem sair da conversa principal.</SheetDescription>
        </SheetHeader>
        {open && <ParallelContent key={sessionId ?? 'unopened'} sessionId={sessionId} />}
      </SheetContent>
    </Sheet>
  )
}
