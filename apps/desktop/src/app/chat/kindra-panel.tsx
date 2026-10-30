import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'

import { PANEL_HOST_URL, PANEL_ORIGIN, PANEL_URL, panelMessage } from './kindra-panel-protocol'
import { ExecutorConversation } from './parallel-executor-view'

interface PanelFrameProps {
  onConversation: (url: string) => void
  onFocus: (value: boolean) => void
}

function PanelFrame({ onConversation, onFocus }: PanelFrameProps) {
  const frame = useRef<HTMLIFrameElement>(null)
  const [route, setRoute] = useState('#/plataforma')
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [src, setSrc] = useState(PANEL_URL + '#/plataforma')
  useEffect(() => {
    let ready = false

    const receive = (event: MessageEvent) => {
      const guest = frame.current?.contentWindow ?? null
      const message = panelMessage(event, guest)

      if (!message) {
        return
      }

      if (message.type === 'ready') {
        ready = true
        setState('ready')
        guest?.postMessage({ type: 'kd-host', url: PANEL_HOST_URL }, PANEL_ORIGIN)
      } else if (message.type === 'conversation') {
        if (message.hash) { setRoute(message.hash) }
        onConversation(message.url)
      } else if (message.type === 'route') {
        setRoute(message.hash)
      } else {
        onFocus(message.on)
        guest?.postMessage({ type: 'kd-focus-state', on: message.on }, PANEL_ORIGIN)
      }
    }

    window.addEventListener('message', receive)

    const timeout = window.setTimeout(() => {
      if (!ready) {
        setState('error')
      }
    }, 15000)

    return () => {
      window.removeEventListener('message', receive)
      window.clearTimeout(timeout)
    }
  }, [attempt, onConversation, onFocus])

  return (
    <div className="relative min-h-0 flex-1">
      {state !== 'ready' && (
        <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-(--ui-chat-bubble-background) text-sm text-muted-foreground" role="status">
          {state === 'loading' ? <Loader label="Abrindo o Painel…" type="lemniscate-bloom" /> : (
            <ErrorState title="Não foi possível conectar ao Painel do Mac mini.">
            <Button onClick={() => { setState('loading'); setSrc(PANEL_URL + route); setAttempt(value => value + 1) }} size="sm" variant="secondary">
              Tentar novamente
            </Button>
            </ErrorState>
          )}
        </div>
      )}
      <iframe
        className="h-full w-full border-0"
        key={attempt}
        onError={() => setState('error')}
        ref={frame}
        referrerPolicy="no-referrer"
        sandbox="allow-scripts allow-same-origin allow-forms"
        src={src}
        title="Kindra Painel"
      />
    </div>
  )
}

/** Keep the panel mounted beneath the conversation drawer, including its current route. */
export function KindraPanel() {
  const [open, setOpen] = useState(false)
  const [focused, setFocused] = useState(false)
  const [conversation, setConversation] = useState<string | null>(null)

  return (
    <Dialog onOpenChange={setOpen} open={open}>
      <DialogTrigger asChild>
        <Button size="sm" variant="ghost">Painel</Button>
      </DialogTrigger>
      <DialogContent bodyClassName="flex h-full min-h-0 flex-col gap-0 overflow-hidden p-0" className={focused ? "h-screen max-h-screen w-screen max-w-none rounded-none" : "h-[92vh] max-h-[92vh] w-[96vw] max-w-[96vw]"}>
        <DialogHeader className="px-5 py-4 pr-12">
          <DialogTitle>Painel</DialogTitle>
          <DialogDescription className="sr-only">Entregas e plataforma do Mac mini. Abra uma conversa para acompanhar em paralelo.</DialogDescription>
        </DialogHeader>
        <PanelFrame onConversation={setConversation} onFocus={setFocused} />
        <Sheet onOpenChange={value => { if (!value) { setConversation(null) } }} open={conversation !== null}>
          <SheetContent className="w-[min(92vw,30rem)] sm:max-w-[30rem] z-[calc(var(--z-modal)+2)]">
            <SheetHeader className="px-6 pt-7 pb-3 pr-12">
              <SheetTitle>Em paralelo</SheetTitle>
              <SheetDescription className="sr-only">Conversa selecionada no Painel. Feche para voltar à tarefa.</SheetDescription>
            </SheetHeader>
            <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6">
              {conversation && <ExecutorConversation key={conversation} url={conversation} />}
            </div>
          </SheetContent>
        </Sheet>
      </DialogContent>
    </Dialog>
  )
}
