import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { cn } from '../../lib/utils'

export type ReportTone = 'positive' | 'warning' | 'muted' | 'destructive'

const TONE_CLASS: Readonly<Record<ReportTone, string>> = {
  positive: 'bg-positive-surface text-positive-ink',
  warning: 'bg-warning-surface text-warning-ink',
  muted: 'bg-muted text-muted-foreground',
  destructive: 'bg-destructive-surface text-destructive-ink',
}

export function ReportToneBadge({ children, className, tone }: { children: ReactNode; className?: string; tone: ReportTone }) {
  return <Badge className={cn('font-semibold', TONE_CLASS[tone], className)}>{children}</Badge>
}
