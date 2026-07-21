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

// Freshness fields for the shell: generation time plus the screening / macro /
// application-DB as-of. Datetime stamps render as JST MM/DD HH:mm; the two store as-of
// values are calendar dates shown as-is. Layout (single line vs wrapping, visibility per
// breakpoint) is left to the caller via `className`.
export function FreshnessMeta({ meta, className }: FreshnessMetaProps) {
  return (
    <div className={cn('flex items-center gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted-foreground', className)}>
      <Field label="生成" value={formatJstStamp(meta.generated_at)} />
      <Field label="screening" value={meta.screening_asof ?? EMPTY} />
      <Field label="macro" value={meta.macro_asof ?? EMPTY} />
      <Field label="DB" value={meta.app_db_updated_at === null ? EMPTY : formatJstStamp(meta.app_db_updated_at)} />
      {meta.batch !== null && <Badge className="font-mono text-[10px] tracking-wider" variant="secondary">{meta.batch}</Badge>}
    </div>
  )
}
