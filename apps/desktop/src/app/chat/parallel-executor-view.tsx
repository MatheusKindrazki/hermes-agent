import { useEffect, useState } from 'react'
import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import type { ParallelLink } from './parallel-links'

type Conversation = {
  id: string
  title: string
  status: string
  truncated?: boolean
  items: Array<{ role: string; text?: string; at: string; tools?: Array<{ title: string; status: string }> }>
}
type Task = {
  title: string
  state: string
  reason?: string
  convs: Array<{ id: string; label: string; url: string }>
  timeline?: Array<{ at: string; text: string }>
}
type Result =
  | { kind: 'task'; live?: boolean; task: Task | null; sourceAt: string | null; publishedAt: string; fetchedAt: string }
  | (Conversation & { kind: 'conversation'; fetchedAt: string })
const text = 'text-xs text-white/40'
const back = 'my-4 text-xs text-white/45 hover:text-white'

/** Only the open, visible panel polls; a failed refresh keeps its last successful snapshot. */
export function useExecutorRead(url: string, enabled = true, poll = true) {
  const visible = usePaneVisible()
  const [data, setData] = useState<Result | null>(null)
  const [error, setError] = useState(false)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    setData(null)
  }, [url])
  useEffect(() => {
    if (!enabled || !visible) return
    let cancelled = false,
      pending = false,
      failures = 0
    const refresh = async () => {
      if (cancelled || pending || failures >= 3 || document.visibilityState === 'hidden') return
      pending = true
      try {
        if (!window.hermesDesktop?.readParallelExecutor) throw new Error('Unavailable bridge')
        const next = (await window.hermesDesktop.readParallelExecutor(url)) as Result
        if (!cancelled) {
          setData(next)
          setError(false)
          failures = 0
        }
      } catch {
        if (!cancelled) {
          failures++
          setError(true)
        }
      } finally {
        pending = false
      }
    }
    void refresh()
    const timer = poll ? window.setInterval(() => void refresh(), 5000) : undefined
    document.addEventListener('visibilitychange', refresh)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [url, enabled, visible, retry, poll])
  return { data, error, retry: () => setRetry(n => n + 1) }
}
function ReadStatus({ read }: { read: ReturnType<typeof useExecutorRead> }) {
  const { data, error } = read
  if (error)
    return (
      <div role="status" className="my-3 text-xs text-amber-200/70">
        Sem conexão com o executor. {data ? 'Exibindo a última leitura.' : 'Não foi possível carregar.'}{' '}
        <button className="underline" onClick={read.retry}>
          Tentar novamente
        </button>
      </div>
    )
  if (!data)
    return (
      <p role="status" className={'my-3 ' + text}>
        Carregando…
      </p>
    )
  if (data.kind === 'task' && !data.live)
    return (
      <p className={'my-3 ' + text}>
        Resumo publicado {data.publishedAt || 'em horário não informado'} · consulta automática
      </p>
    )
  return (
    <p role="status" className={'my-3 ' + text}>
      Atualização a cada 5 s · última leitura {new Date(data.fetchedAt).toLocaleTimeString('pt-BR')}
    </p>
  )
}
const states: Record<string, string> = {
  RUNNING: 'Em andamento',
  IDLE: 'Em espera',
  FINISHED: 'Concluída',
  STOPPED: 'Parada',
  ERROR: 'Falhou',
  PAUSED: 'Pausada',
  AWAITING_USER_INPUT: 'Aguardando resposta'
}
function ExecutorConversation({ url }: { url: string }) {
  const read = useExecutorRead(url)
  const conversation = read.data?.kind === 'conversation' ? read.data : null
  return (
    <div>
      <h3 className="text-sm font-medium text-white/85">{conversation?.title || 'Conversa do agente'}</h3>
      {conversation && (
        <p className={'mt-1 ' + text}>{states[conversation.status?.toUpperCase()] || conversation.status}</p>
      )}
      <ReadStatus read={read} />
      {conversation?.truncated && (
        <p className={'mb-4 ' + text}>Trecho recente da conversa; o histórico foi limitado pelo executor.</p>
      )}
      <div className="space-y-5 pb-5">
        {conversation && conversation.items.length === 0 && (
          <p className={text}>Ainda não há mensagens nesta conversa.</p>
        )}
        {conversation?.items
          .slice()
          .reverse()
          .map((item, index) => (
            <div key={item.at + ':' + index}>
              <p className="mb-1.5 text-[11px] text-white/35">
                {item.role === 'work' ? 'Atividade' : item.role === 'agent' ? 'Agente' : 'Instrução'} ·{' '}
                {item.at.replace('T', ' ')}
              </p>
              {item.role === 'work' ? (
                <details className={text}>
                  <summary className="cursor-pointer">{item.tools?.length || 0} ações · ver detalhes</summary>
                  <ul className="mt-2 space-y-2">
                    {item.tools?.map((tool, i) => (
                      <li key={i}>
                        {tool.title}
                        <span className="ml-2 text-white/30">{tool.status}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : (
                <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-white/75">{item.text}</p>
              )}
            </div>
          ))}
      </div>
    </div>
  )
}
function ExecutorTask({ link }: { link: ParallelLink }) {
  const read = useExecutorRead(link.url)
  const task = read.data?.kind === 'task' ? read.data.task : null
  const [conversation, setConversation] = useState<{ url: string; label: string } | null>(null)
  if (conversation)
    return (
      <div>
        <button className={back} onClick={() => setConversation(null)}>
          ← Voltar à tarefa
        </button>
        <ExecutorConversation key={conversation.url} url={conversation.url} />
      </div>
    )
  return (
    <div>
      <h3 className="text-sm font-medium text-white/85">{task?.title || link.title}</h3>
      {task && <p className="mt-2 text-xs text-white/65">{task.state}</p>}
      <ReadStatus read={read} />
      {read.data && !task && (
        <p className={text}>Esta tarefa não está no resumo atual do executor. Isso não confirma que foi concluída.</p>
      )}
      {task?.reason && <p className="my-4 whitespace-pre-wrap text-xs leading-relaxed text-white/55">{task.reason}</p>}
      {task && (
        <div className="divide-y divide-white/6">
          {task.convs.map(conv => (
            <button
              key={conv.id}
              className="flex w-full items-center justify-between py-4 text-left text-sm text-white/80"
              onClick={() => setConversation(conv)}
            >
              {conv.label}
              <span className="text-white/30">›</span>
            </button>
          ))}
          {task.convs.length === 0 && <p className={'py-4 ' + text}>Nenhuma conversa vinculada pelo executor.</p>}
        </div>
      )}
      {!!task?.timeline?.length && (
        <details className="mt-5 text-xs text-white/40">
          <summary className="cursor-pointer">Últimos acontecimentos</summary>
          <ul className="mt-4 space-y-4">
            {task.timeline.map((event, i) => (
              <li key={i}>
                <p className="mb-1 text-white/30">{event.at}</p>
                {event.text}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
function TaskRow({ link, onOpen }: { link: ParallelLink; onOpen: () => void }) {
  const read = useExecutorRead(link.url)
  const task = read.data?.kind === 'task' ? read.data.task : null
  return (
    <button onClick={onOpen} className="flex w-full items-center gap-3 border-b border-white/6 py-4 text-left">
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 text-sm text-white/80">{task?.title || link.title}</span>
        <span className="mt-1 block text-xs text-white/40">
          {read.error
            ? 'Sem conexão · última leitura'
            : (task ? task.state + (read.data?.kind === 'task' && !read.data.live ? ' · resumo' : '') : '') ||
              (read.data ? 'Fora do resumo atual' : 'Carregando estado…')}
        </span>
      </span>
      <span className="text-white/30">›</span>
    </button>
  )
}
function ConversationRow({ link, onOpen }: { link: ParallelLink; onOpen: () => void }) {
  const read = useExecutorRead(link.url, true, false)
  const conversation = read.data?.kind === 'conversation' ? read.data : null
  const latest = conversation?.items.filter(item => item.text).at(-1)?.text
  return (
    <button onClick={onOpen} className="flex w-full items-center gap-3 border-b border-white/6 py-4 text-left">
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 text-sm text-white/80">{conversation?.title || 'Conversa do agente'}</span>
        <span className="mt-1 block truncate text-xs text-white/40">
          {read.error
            ? 'Sem conexão · tentar abrir'
            : latest || (conversation ? 'Ver mensagens e atividade' : 'Carregando conversa…')}
        </span>
      </span>
      <span className="text-white/30">›</span>
    </button>
  )
}
export function ExecutorLinks({ links }: { links: ParallelLink[] }) {
  const [selected, setSelected] = useState<ParallelLink | null>(null)
  const [more, setMore] = useState(false)
  if (!links.length) return null
  if (selected)
    return (
      <section className="pb-4">
        <button className={back} onClick={() => setSelected(null)}>
          ← Voltar à lista
        </button>
        {selected.kind === 'task' ? (
          <ExecutorTask key={selected.url} link={selected} />
        ) : (
          <ExecutorConversation key={selected.url} url={selected.url} />
        )}
      </section>
    )
  return (
    <section className="pb-3">
      <p className="mt-5 mb-1 text-[11px] text-white/35">No executor · referências desta conversa</p>
      {(more ? links : links.slice(0, 8)).map(link =>
        link.kind === 'task' ? (
          <TaskRow key={link.url} link={link} onOpen={() => setSelected(link)} />
        ) : (
          <ConversationRow key={link.url} link={link} onOpen={() => setSelected(link)} />
        )
      )}
      {!more && links.length > 8 && (
        <button className={back} onClick={() => setMore(true)}>
          Ver mais
        </button>
      )}
    </section>
  )
}
