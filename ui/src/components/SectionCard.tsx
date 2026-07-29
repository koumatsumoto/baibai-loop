import type { ReactNode } from 'react'

import { InfoHint } from './InfoHint'
import { Card, CardDescription, CardHeader, CardTitle } from './ui/card'
import { cn } from '../lib/utils'

interface SectionCardProps {
  /** Also names the ⓘ for assistive technology, so it stays a plain string. */
  title: string
  /** Why this section is on the page. Drawn behind an ⓘ beside the title. */
  hint?: ReactNode
  /** One line under the title: what the rows are. */
  description?: ReactNode
  /** Counts, totals or badges, right-aligned on the title's row. */
  meta?: ReactNode
  /** Rendered flush under the rule — a table, a divided list, or a CardContent. */
  children: ReactNode
  /** 2 directly under the page title, 3 for a card nested inside an `h2` section. */
  headingLevel?: 2 | 3
  className?: string
}

// One grammar for every list on the site: a ruled header carrying the title, why it is
// here, and the numbers that summarise it, over content that runs to the card's edges.
// Sections that share a shape are read as one system rather than as a pile of panels.
export function SectionCard({ title, hint, description, meta, children, headingLevel = 2, className }: SectionCardProps) {
  return (
    <Card className={cn('gap-0 overflow-hidden py-0 shadow-sm', className)}>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b px-5 py-4 sm:px-6">
        <div className="min-w-0">
          {/* The card title is the section's heading, so it is one in the accessibility
              tree too and a screen reader can jump between sections. */}
          <CardTitle aria-level={headingLevel} className="flex items-center gap-1.5" role="heading">
            {title}
            {hint !== undefined && <InfoHint label={title}>{hint}</InfoHint>}
          </CardTitle>
          {description !== undefined && <CardDescription className="mt-1">{description}</CardDescription>}
        </div>
        {meta}
      </CardHeader>
      {children}
    </Card>
  )
}
