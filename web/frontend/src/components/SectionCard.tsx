import type { ReactNode } from 'react'

import { InfoHint } from './InfoHint'
import { Card, CardDescription, CardHeader, CardTitle } from './ui/card'

interface SectionCardProps {
  /** Also names the ⓘ for assistive technology, so it stays a plain string. */
  title: string
  /** Why this section is on the page. Drawn behind an ⓘ beside the title. */
  hint?: ReactNode
  /** One line under the title: what the rows are. No counts — those go in `meta`. */
  description?: ReactNode
  /** Counts, totals or badges, right-aligned on the title's row. */
  meta?: ReactNode
  /**
   * Inset the content to the header's own margin. Tables and divided lists run to the
   * card's edges and leave this off; a block of prose or fields turns it on. The card
   * owns both paddings so a section cannot align its content against its own title.
   */
  padded?: boolean
  children: ReactNode
  /** 2 directly under the page title, 3 for a card nested inside an `h2` section. */
  headingLevel?: 2 | 3
}

// One grammar for every section on the site: a ruled header carrying the title, why it
// is here, and the numbers that summarise it, over the content it introduces. Sections
// that share a shape are read as one system rather than as a pile of panels.
//
// A section is a top-level part of a page. The cards *inside* one — a lane, a candidate,
// a scenario — are items, not sections: they carry their own emphasis (a tinted header,
// a coloured border) and are not headings in the accessibility tree, so they stay
// hand-written rather than being forced through here.
export function SectionCard({ title, hint, description, meta, padded = false, children, headingLevel = 2 }: SectionCardProps) {
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      {/* The bottom padding comes from the primitive's own `[.border-b]` rule, which
          outranks a `py-*` utility; matching the top to the same variable keeps the
          two from silently diverging. */}
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b px-5 pt-(--card-spacing) sm:px-6">
        <div className="min-w-0">
          {/* The card title is the section's heading, so it is one in the accessibility
              tree too and a screen reader can jump between sections. The ⓘ sits beside
              it rather than inside, or its label is read as part of the heading. */}
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
