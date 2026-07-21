import type { MetaView } from '../api/types'
import { Badge } from './ui/badge'
import { EMPTY, formatJstStamp } from '../lib/format'
import { cn } from '../lib/utils'

interface FreshnessMetaProps {
  meta: MetaView
  className?: string
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1">
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70">{label}</span>
      <span className="tabular-nums">{value}</span>
    </span>
  )
}

// Compact freshness strip for the shell header: generation time plus the screening /
// macro / application-DB as-of. Datetime stamps render as JST MM/DD HH:mm; the two
// store as-of values are calendar dates shown as-is.
export function FreshnessMeta({ meta, className }: FreshnessMetaProps) {
  return (
    <div className={cn('hidden items-center gap-3 border-r border-border/60 pr-3 font-mono text-[11px] text-muted-foreground lg:flex', className)}>
      <Field label="生成" value={formatJstStamp(meta.generated_at)} />
      <Field label="screening" value={meta.screening_asof ?? EMPTY} />
      <Field label="macro" value={meta.macro_asof ?? EMPTY} />
      <Field label="DB" value={meta.app_db_updated_at === null ? EMPTY : formatJstStamp(meta.app_db_updated_at)} />
      {meta.batch !== null && <Badge className="font-mono text-[10px] tracking-wider" variant="secondary">{meta.batch}</Badge>}
    </div>
  )
}
