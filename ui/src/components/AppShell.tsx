import { Link, NavLink } from 'react-router-dom'

import { ThemeToggle } from './ThemeToggle'
import { Badge } from './ui/badge'
import { cn } from '../lib/utils'

export function AppShell() {
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
        <div className="ml-auto flex items-center gap-2">
          <Badge className="hidden font-mono text-[10px] tracking-wider sm:inline-flex" variant="secondary">READ ONLY</Badge>
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}
