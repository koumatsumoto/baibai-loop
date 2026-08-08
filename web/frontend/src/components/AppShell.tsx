import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router'
import { Activity, ExternalLink, Settings } from 'lucide-react'

import { fetchJson } from '../api/client'
import type { MetaView } from '../api/types'
import { BrandMark } from './BrandMark'
import { FreshnessMeta } from './FreshnessMeta'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'
import { ACTIONS_URL, NAV_TABS } from '../lib/nav'
import { cn } from '../lib/utils'

// Operational surfaces live behind the gear, apart from the judgment tabs: the
// three tabs answer "what should I do", this menu answers "is the machinery ok".
function DevMenu() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button aria-label="開発メニュー" size="icon-sm" variant="ghost">
          <Settings aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>開発</DropdownMenuLabel>
        <DropdownMenuItem asChild>
          <Link to="/system"><Activity aria-hidden="true" />システム状態</Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <a href={ACTIONS_URL} rel="noreferrer noopener" target="_blank">
            <ExternalLink aria-hidden="true" />GitHub Actions
          </a>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function AppShell() {
  const [meta, setMeta] = useState<MetaView | null>(null)
  const { pathname } = useLocation()

  // Meta view is optional infrastructure: when the endpoint is missing or errors, keep
  // the shell usable and simply omit the freshness strip.
  useEffect(() => {
    fetchJson<MetaView>('/api/meta').then(setMeta).catch(() => setMeta(null))
  }, [])

  return (
    <header className="sticky top-0 z-40 border-b bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/88">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-2 px-4 sm:gap-6 sm:px-6 lg:px-8">
        <Link aria-label="Baibai Loop ホーム" className="flex shrink-0 items-center gap-2.5 font-semibold tracking-tight" to="/">
          <BrandMark />
          <span className="hidden sm:inline">Baibai Loop</span>
        </Link>
        <nav className="flex h-full min-w-0 flex-1 items-center gap-0 overflow-x-auto sm:gap-1" aria-label="メインナビゲーション">
          {NAV_TABS.map((item) => {
            const active = item.match(pathname)
            return (
              <Link
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'relative flex h-full items-center px-2 text-sm font-medium text-muted-foreground transition-colors hover:text-primary sm:px-3',
                  active && 'text-primary after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:rounded-full after:bg-primary',
                )}
                key={item.to}
                to={item.to}
              >
                {item.label}
              </Link>
            )
          })}
        </nav>
        <div className="flex shrink-0 items-center gap-3">
          {meta !== null && <FreshnessMeta className="hidden border-r border-border/60 pr-3 lg:flex" deployedAt={import.meta.env.VITE_DEPLOYED_AT} meta={meta} />}
          <Badge className="hidden font-mono text-[10px] tracking-wider sm:inline-flex" variant="secondary">READ ONLY</Badge>
          <DevMenu />
        </div>
      </div>
      {meta !== null && (
        <div className="mx-auto max-w-[1600px] border-t px-4 py-1.5 sm:px-6 lg:hidden">
          <FreshnessMeta className="flex-wrap justify-start" deployedAt={import.meta.env.VITE_DEPLOYED_AT} meta={meta} />
        </div>
      )}
    </header>
  )
}
