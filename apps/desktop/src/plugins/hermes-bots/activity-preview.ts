import { atom, host, useValue } from '@hermes/plugin-sdk'

import { botCanonicalSessionId } from './row-helpers'
import type { RosterRow } from './types'

const emptyStatus = atom<Readonly<Record<string, string>>>({})
const emptyTools = atom<Readonly<Record<string, readonly string[]>>>({})

export function useActivityStatus() {
  return useValue(host.state.sessionStatusById || emptyStatus)
}

export function useActivityTools() {
  return useValue(host.state.sessionToolsById || emptyTools)
}

/** Describe observable operations, without inferring goals from raw commands. */
export function activityPreview(
  bot: RosterRow,
  mood: string,
  statuses: Readonly<Record<string, string>>,
  tools: Readonly<Record<string, readonly string[]>>,
  attention = false
): string | null {
  const id = botCanonicalSessionId(bot)
  const status = id ? statuses[id] : undefined

  if (status === 'needs-input') {
    return 'Aguardando sua resposta'
  }

  if (attention) {
    return 'Precisa da sua atenção'
  }

  if (status === 'stalled') {
    return 'Sem progresso recente'
  }

  if (mood === 'think') {
    return 'Pensando na próxima etapa'
  }

  if (mood === 'work') {
    const names = id ? tools[id] || [] : []

    return describeActivityTools(names)
  }

  return null
}

function describeActivityTools(names: readonly string[]): string {
  if (!names.length) {
    return 'Trabalhando em segundo plano'
  }

  const reads = names.every(name => /read_file|readfile|file_read/.test(name))
  const writes = names.every(name => /write_file|file_write|edit_file|apply_patch|file_edit/.test(name))
  const computer = names.every(name => /computer|cua_|cua\b|desktop.*(?:click|type|capture)/.test(name))
  const tests = names.every(name => /(?:^|[_.:])(?:run_tests|test_runner|execute_tests)(?:$|[_.:])/.test(name))
  const searches = names.every(name => /search|find/.test(name))
  const browser = names.every(name => /browser|navigate|screenshot/.test(name))
  const commands = names.every(name => /terminal|exec|command/.test(name))

  if (reads) {
    return names.length === 1 ? 'Lendo um arquivo' : `Lendo ${names.length} arquivos`
  }

  if (writes) {
    return names.length === 1 ? 'Editando um arquivo' : `Editando ${names.length} arquivos`
  }

  if (computer) {
    return 'Operando um aplicativo'
  }

  if (tests) {
    return 'Rodando testes'
  }

  if (searches) {
    return 'Buscando informações'
  }

  if (browser) {
    return 'Navegando e verificando'
  }

  if (commands) {
    return names.length === 1 ? 'Executando um comando' : `Executando ${names.length} comandos`
  }

  return names.length === 1 ? 'Executando uma ação' : `Executando ${names.length} ações`
}
