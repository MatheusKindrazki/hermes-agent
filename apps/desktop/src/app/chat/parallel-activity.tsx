import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { useSessionSlice } from '@/lib/use-session-slice'
import { knownOwnerForSession, requestForOwnedSession } from '@/store/session-states'
import { $subagentsBySession, reconcileSubagentSnapshot, type SubagentPayload } from '@/store/subagents'

import { SubagentTranscript } from './composer/status-stack/subagent-transcript'
import { rejectUnownedSubagentRequest } from './composer/status-stack/use-subagent-snapshot'
import { agentExchanges } from './parallel-exchanges'
import { ExecutorLinks } from './parallel-executor-view'
import { parallelLinks } from './parallel-links'
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
    if (!sessionId || !visible) {return}
    let cancelled = false
    let pending = false
    let failures = 0

    const refresh = async () => {
      if (cancelled || pending || failures >= 3 || document.visibilityState === 'hidden') {return}
      pending = true
      const before = $subagentsBySession.get()[sessionId]
      const owner = JSON.stringify(knownOwnerForSession(sessionId))

      try {
        const snapshot = await requestForOwnedSession<{
          subagents: SubagentPayload[]
          delegations?: SubagentPayload[]
        }>(sessionId, rejectUnownedSubagentRequest, 'subagent.list', { session_id: sessionId })

        if (!Array.isArray(snapshot.subagents)) {throw new Error('Invalid agent snapshot')}

        if (!cancelled && owner === JSON.stringify(knownOwnerForSession(sessionId))) {
          if (before === $subagentsBySession.get()[sessionId]) {
            reconcileSubagentSnapshot(sessionId, snapshot.subagents, snapshot.delegations ?? [])
          }

          setLoad('ready')
          failures = 0
        }
      } catch {
        failures++

        if (!cancelled) {setLoad('error')}
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

  const [tab, setTab] = useState<'conversations' | 'tasks'>('conversations')
  const [peer, setPeer] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const groups = useMemo(() => {
    const map = new Map<string, { key: string; name: string; rows: typeof exchanges }>()

    for (const entry of exchanges) {
      const key = entry.peer.toLowerCase().replace(/^@/, '')
      const group = map.get(key) ?? { key, name: entry.name, rows: [] }

      if (entry.label.startsWith('De ')) {group.name = entry.name}
      group.rows.push(entry)
      map.set(key, group)
    }

    return [...map.values()]
  }, [exchanges])

  const conversation = groups.find(group => group.key === peer)

  return (
    <>
      <div aria-label="Em paralelo" className="flex gap-6 border-b border-white/8 px-6" role="tablist">
        {(
          [
            ['conversations', 'Conversas'],
            ['tasks', 'Tarefas']
          ] as const
        ).map(([value, label]) => (
          <button
            aria-selected={tab === value}
            className={
              'border-b-2 pb-3 text-sm transition-colors ' +
              (tab === value ? 'border-white/75 text-white' : 'border-transparent text-white/40 hover:text-white/70')
            }
            key={value}
            onClick={() => setTab(value)}
            role="tab"
            type="button"
          >
            {label}
          </button>
        ))}
      </div>
      <div
        aria-label={tab === 'conversations' ? 'Conversas' : 'Tarefas'}
        className="min-h-0 flex-1 overflow-y-auto px-6 pb-6"
        role="tabpanel"
      >
        {tab === 'conversations' ? (
          conversation ? (
            <div>
              <button
                className="my-4 text-xs text-white/45 hover:text-white"
                onClick={() => setPeer(null)}
                type="button"
              >
                ← Todas as conversas
              </button>
              <h3 className="mb-5 text-sm font-medium text-white/85">{conversation.name}</h3>
              <div className="space-y-5">
                {conversation.rows.map(entry => (
                  <div className="space-y-1.5" key={entry.id}>
                    <p className="text-[11px] text-white/40">
                      {entry.label.startsWith('De ') ? conversation.name : 'Este personagem'}
                    </p>
                    <button
                      aria-expanded={expanded === entry.id}
                      className="block w-full text-left"
                      onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
                      type="button"
                    >
                      <p
                        className={
                          'whitespace-pre-wrap break-words text-[13px] leading-relaxed text-white/75 ' +
                          (expanded === entry.id ? '' : 'line-clamp-3')
                        }
                      >
                        {entry.text}
                      </p>
                      <span className="mt-1 block text-[11px] text-white/35">
                        {expanded === entry.id ? 'Recolher' : 'Ler mensagem'}
                      </span>
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="divide-y divide-white/6">
              <ExecutorLinks links={links.filter(link => link.kind === 'conversation')} />
              {groups.length === 0 ? (
                <p className="py-8 text-sm text-white/40">Ainda não há trocas entre personagens neste trecho.</p>
              ) : (
                groups.map(group => (
                  <button
                    className="flex w-full items-center gap-3 py-5 text-left"
                    key={group.key}
                    onClick={() => {
                      setPeer(group.key)
                      setExpanded(null)
                    }}
                    type="button"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-white/85">{group.name}</span>
                      <span className="mt-1 block truncate text-xs text-white/40">{group.rows[0].text}</span>
                    </span>
                    <span aria-hidden className="text-white/30">
                      ›
                    </span>
                  </button>
                ))
              )}
            </div>
          )
        ) : (
          <div className="py-4">
            {load === 'error' && (
              <div className="mb-4 text-xs text-white/45" role="status">
                <p>Não foi possível atualizar. Os registros podem estar desatualizados.</p>
                <button className="mt-2 underline" onClick={() => setRetry(value => value + 1)} type="button">
                  Tentar novamente
                </button>
              </div>
            )}
            {load === 'loading' && (
              <p className="mb-4 text-xs text-white/40" role="status">
                Atualizando…
              </p>
            )}
            {agents.map(agent => (
              <div className="border-b border-white/6 py-3" key={agent.id}>
                <button
                  aria-expanded={selected === agent.id}
                  className="w-full text-left"
                  onClick={() => setSelected(selected === agent.id ? null : agent.id)}
                  type="button"
                >
                  <span className="block text-sm text-white/85">{agent.goal || 'Tarefa do agente'}</span>
                  <span className="mt-1 block text-xs text-white/40">{statuses[agent.status]} · Ver conversa</span>
                </button>
                {selected === agent.id && sessionId && (
                  <SubagentTranscript key={sessionId + ':' + agent.id} sessionId={sessionId} subagentId={agent.id} />
                )}
              </div>
            ))}
            <ExecutorLinks links={links.filter(link => link.kind === 'task')} />
            {load === 'ready' && agents.length === 0 && links.length === 0 && (
              <p className="py-4 text-sm text-white/40">Nenhuma tarefa disponível neste trecho.</p>
            )}
          </div>
        )}
      </div>
    </>
  )
}

export function ParallelActivity() {
  const view = useSessionView()
  const sessionId = useStore(view.$runtimeId)
  const [open, setOpen] = useState(false)
  useEffect(() => setOpen(false), [sessionId])

  return (
    <Sheet onOpenChange={setOpen} open={open}>
      <SheetTrigger asChild>
        <Button className="shrink-0 text-xs text-muted-foreground" size="sm" variant="ghost">
          Em paralelo
        </Button>
      </SheetTrigger>
      <SheetContent
        className="w-[min(92vw,27rem)] sm:max-w-[27rem]"
        style={{ backgroundColor: '#171719', color: '#eee', zIndex: 'var(--z-modal)' }}
      >
        <SheetHeader className="px-6 pt-7 pb-3 pr-12">
          <SheetTitle>Em paralelo</SheetTitle>
          <SheetDescription className="sr-only">
            Tarefas e conversas dos agentes, sem sair da conversa principal.
          </SheetDescription>
        </SheetHeader>
        {open && <ParallelContent key={sessionId ?? 'unopened'} sessionId={sessionId} />}
      </SheetContent>
    </Sheet>
  )
}
