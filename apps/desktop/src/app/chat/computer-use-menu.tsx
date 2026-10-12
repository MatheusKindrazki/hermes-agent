import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import { ComputerUsePanel } from '@/app/settings/computer-use-panel'
import { ToolsetConfigPanel } from '@/app/settings/toolset-config-panel'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { $localApplications, loadLocalApplications } from '@/lib/local-applications'
import { resolveBotConnectionRoute } from '@/plugins/hermes-bots/routing'

import { useChatPersona } from './bot-persona'

export function ComputerUseMenu({
  sessionId,
  open,
  onOpenChange
}: {
  sessionId?: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const catalog = useStore($localApplications)
  const persona = useChatPersona(sessionId ?? null)
  const route = persona ? resolveBotConnectionRoute(persona.bot).route : null
  const connectionId = route?.connectionId
  const profileName = route?.profile

  const profile = useMemo(
    () => (profileName ? { connectionId, profile: profileName } : undefined),
    [connectionId, profileName]
  )

  const scopeLabel = persona?.bot.connectionLabel || 'Conexão desta conversa'
  useEffect(() => {
    if (open) {
      void loadLocalApplications()
    }
  }, [open])

  return (
    <>
      <Dialog onOpenChange={onOpenChange} open={open}>
        <DialogContent className="max-h-[80vh] max-w-lg overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Computer Use</DialogTitle>
            <DialogDescription>{scopeLabel}</DialogDescription>
          </DialogHeader>
          <section className="grid gap-3">
            <p className="text-sm text-muted-foreground">
              Aplicativos no @ vêm deste Mac{catalog ? ` · ${catalog.machine}` : ''}. A conexão abaixo informa qual
              computador o bot pode operar.
            </p>
            {catalog && (
              <p className="text-xs text-muted-foreground">{catalog.apps.length} aplicativos disponíveis no menu @</p>
            )}
            <ComputerUsePanel key={JSON.stringify(profile)} profile={profile} />
            <ToolsetConfigPanel key={`config:${JSON.stringify(profile)}`} profile={profile} toolset="computer_use" />
          </section>
        </DialogContent>
      </Dialog>
    </>
  )
}
