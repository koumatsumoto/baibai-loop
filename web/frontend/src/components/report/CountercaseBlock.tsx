import { CircleAlert } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '../../lib/utils'

export function CountercaseBlock({ children, className, label = '最も強い反対仮説' }: { children: ReactNode; className?: string; label?: ReactNode }) {
  return (
    <aside className={cn('grid gap-1.5 rounded-md bg-warning-surface p-3 text-warning-ink', className)} role="note">
      <div className="flex items-center gap-1.5 text-xs font-semibold"><CircleAlert aria-hidden="true" className="size-3.5" />{label}</div>
      <div className="text-sm text-foreground">{children}</div>
    </aside>
  )
}
