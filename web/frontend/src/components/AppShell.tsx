import { Link, useLocation } from 'react-router'
import { ExternalLink, FileText, LayoutDashboard, Settings } from 'lucide-react'

import { BrandMark } from './BrandMark'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'
import { ACTIONS_URL, NAV_TABS } from '../lib/nav'
import { cn } from '../lib/utils'

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
          <a href={ACTIONS_URL} rel="noreferrer noopener" target="_blank">
            <ExternalLink aria-hidden="true" />GitHub Actions
          </a>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function AppShell() {
  const { pathname } = useLocation()

  return (
    <header className="sticky top-0 z-40 border-b bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/88">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-2 px-4 sm:gap-6 sm:px-6 lg:px-8">
        <Link aria-label="Baibai Loop ホーム" className="flex shrink-0 items-center gap-2.5 rounded-md font-semibold tracking-tight focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" to="/">
          <BrandMark />
          <span className="hidden sm:inline">Baibai Loop</span>
        </Link>
        <nav className="grid min-w-0 flex-1 grid-cols-2 gap-1 rounded-lg bg-muted p-1 sm:w-64 sm:flex-none" aria-label="メインナビゲーション">
          {NAV_TABS.map((item) => {
            const active = item.match(pathname)
            return (
              <Link
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex min-h-10 min-w-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:px-4',
                  active ? 'bg-surface text-primary shadow-sm' : 'text-muted-foreground hover:bg-surface/60 hover:text-foreground',
                )}
                key={item.to}
                to={item.to}
              >
                {item.to === '/' ? <LayoutDashboard aria-hidden="true" className="size-4 shrink-0" /> : <FileText aria-hidden="true" className="size-4 shrink-0" />}
                {item.label}
              </Link>
            )
          })}
        </nav>
        <div className="ml-auto flex shrink-0 items-center gap-3">
          <Badge className="hidden font-mono text-[10px] tracking-wider sm:inline-flex" variant="secondary">READ ONLY</Badge>
          <DevMenu />
        </div>
      </div>
    </header>
  )
}
