import { useMemo } from 'react'
import { Codicon } from '@/components/ui/codicon'
import { useContributions } from '@/contrib/react/use-contributions'
import { SIDEBAR_NAV_AREA, type SidebarNavContribution } from '../../routes'
import type { SidebarNavItem } from '../../types'

export function useContributedSidebarNav(): SidebarNavItem[] {
  const navContributions = useContributions(SIDEBAR_NAV_AREA)

  const contributedNav = useMemo<SidebarNavItem[]>(
    () =>
      navContributions.flatMap(c => {
        const data = c.data as Partial<SidebarNavContribution> | undefined

        if (!data?.path?.startsWith('/') || !data.label) {
          return []
        }

        const codicon = data.codicon || 'plug'

        return [
          {
            id: c.id,
            label: data.label,
            icon: (props: { className?: string }) => <Codicon name={codicon} {...props} />,
            route: data.path,
            tier: data.tier
          }
        ]
      }),
    [navContributions]
  )

  return contributedNav
}
