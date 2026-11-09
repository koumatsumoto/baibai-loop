import type { ReactNode } from 'react'

import { InfoHint } from './InfoHint'
import { Card, CardDescription, CardHeader, CardTitle } from './ui/card'

interface SectionCardProps {
  /** Also names the ⓘ for assistive technology, so it stays a plain string. */
  title: string
  hint?: ReactNode
  description?: ReactNode
  meta?: ReactNode
  padded?: boolean
  children: ReactNode
  /** 2 directly under the page title, 3 for a card nested inside an `h2` section. */
  headingLevel?: 2 | 3
}

export function SectionCard({ title, hint, description, meta, padded = false, children, headingLevel = 2 }: SectionCardProps) {
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      {/* Match the primitive's border-specific bottom padding. */}
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b px-5 pt-(--card-spacing) sm:px-6">
        <div className="min-w-0">
          {/* Keep the hint outside the heading's accessible name. */}
          <div className="flex items-center gap-1.5">
            <CardTitle aria-level={headingLevel} role="heading">{title}</CardTitle>
            {hint !== undefined && <InfoHint label={title}>{hint}</InfoHint>}
          </div>
          {description !== undefined && <CardDescription className="mt-1">{description}</CardDescription>}
        </div>
        {meta}
      </CardHeader>
      {padded ? <div className="px-5 py-4 sm:px-6">{children}</div> : children}
    </Card>
  )
}
