import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { fetchJson } from '../api/client'
import type { MetaView } from '../api/types'
import { BrandMark } from './BrandMark'
import { FreshnessMeta } from './FreshnessMeta'
import { ThemeToggle } from './ThemeToggle'
import { Badge } from './ui/badge'
import { NAV_TABS } from '../lib/nav'
import { cn } from '../lib/utils'

export function AppShell() {
  const [meta, setMeta] = useState<MetaView | null>(null)
  const { pathname } = useLocation()

  // Meta view is optional infrastructure: when the endpoint is missing or errors, keep
  // the shell usable and simply omit the freshness strip.
  useEffect(() => {
    fetchJson<MetaView>('/api/meta').then(setMeta).catch(() => setMeta(null))
  }, [])

  return (
    <header className="sticky top-0 z-40 border-b bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/88 dark:bg-background/95 dark:supports-[backdrop-filter]:bg-background/88">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-2 px-4 sm:gap-6 sm:px-6 lg:px-8">
        <Link aria-label="Baibai App ホーム" className="flex shrink-0 items-center gap-2.5 font-semibold tracking-tight" to="/">
          <BrandMark />
          <span className="hidden sm:inline">Baibai App</span>
        </Link>
        <nav className="flex h-full min-w-0 flex-1 items-center gap-0 overflow-x-auto sm:gap-1" aria-label="メインナビゲーション">
          {NAV_TABS.map((item) => {
            const active = item.match(pathname)
            return (
              <Link
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'relative flex h-full items-center px-2 text-sm font-medium text-muted-foreground transition-colors hover:text-primary sm:px-3',
                  active && 'text-primary after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:rounded-full after:bg-primary-display',
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
          {meta !== null && <FreshnessMeta className="hidden border-r border-border/60 pr-3 lg:flex" meta={meta} />}
          <Badge className="hidden font-mono text-[10px] tracking-wider sm:inline-flex" variant="secondary">READ ONLY</Badge>
          <ThemeToggle />
        </div>
      </div>
      {meta !== null && (
        <div className="mx-auto max-w-[1600px] border-t px-4 py-1.5 sm:px-6 lg:hidden">
          <FreshnessMeta className="flex-wrap justify-start" meta={meta} />
        </div>
      )}
    </header>
  )
}
