import { useEffect, useState } from 'react'
import { Link, NavLink } from 'react-router-dom'

import { fetchJson } from '../api/client'
import type { MetaView } from '../api/types'
import { FreshnessMeta } from './FreshnessMeta'
import { ThemeToggle } from './ThemeToggle'
import { Badge } from './ui/badge'
import { cn } from '../lib/utils'

export function AppShell() {
  const [meta, setMeta] = useState<MetaView | null>(null)

  // Meta view is optional infrastructure: when the endpoint is missing or errors, keep
  // the shell usable and simply omit the freshness strip.
  useEffect(() => {
    fetchJson<MetaView>('/api/meta').then(setMeta).catch(() => setMeta(null))
  }, [])

  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/85">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-6 px-4 sm:px-6 lg:px-8">
        <Link className="flex shrink-0 items-center gap-2 font-semibold tracking-tight" to="/">
          <span className="grid size-7 place-items-center rounded-lg bg-foreground text-[10px] font-bold tracking-wide text-background">BL</span>
          <span>Baibai-Loop</span>
        </Link>
        <nav className="flex h-full items-center gap-1" aria-label="メインナビゲーション">
          {[
            { to: '/', label: 'Dashboard', end: true },
            { to: '/screening', label: 'Screening', end: false },
            { to: '/shortlist', label: 'Shortlist', end: false },
            { to: '/macro', label: 'Macro', end: false },
          ].map((item) => (
            <NavLink
              className={({ isActive }) => cn(
                'relative flex h-full items-center px-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground',
                isActive && 'text-foreground after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:rounded-full after:bg-foreground',
              )}
              end={item.end}
              key={item.to}
              to={item.to}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3">
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
