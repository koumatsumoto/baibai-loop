import type { ReactNode } from 'react'

import { cn } from '../lib/utils'

// How wide the measure is — the only difference between pages that carries meaning. A
// grid of panels wants the viewport; a page about one subject, read top to bottom,
// wants a line short enough to track. Spacing, padding and the heading are the same
// everywhere, so a new page cannot drift from the others by accident.
const WIDTH_CLASS = {
  wide: 'max-w-[1600px]',
  reading: 'max-w-[1280px]',
} as const

interface PageShellProps {
  title: ReactNode
  /** One sentence under the title, when the name alone does not say what the page is for. */
  lead?: ReactNode
  /** Freshness, counts or controls, aligned to the title's baseline. */
  meta?: ReactNode
  /** Breadcrumb or back link, above the title. */
  above?: ReactNode
  width?: keyof typeof WIDTH_CLASS
  children: ReactNode
}

export function PageShell({ title, lead, meta, above, width = 'wide', children }: PageShellProps) {
  return (
    <main className={cn('mx-auto grid gap-6 px-4 py-6 sm:px-6 lg:px-8 lg:py-8', WIDTH_CLASS[width])}>
      {above}
      {/* Wrapping rather than a breakpoint switch: the meta drops under the title when
          it no longer fits, at whatever width that happens to be for this page. */}
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {lead !== undefined && <p className="mt-1 text-sm text-muted-foreground">{lead}</p>}
        </div>
        {meta}
      </header>
      {children}
    </main>
  )
}
