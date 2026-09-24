import type { MetaView } from '../api/types'
import { formatJstStamp } from '../lib/format'
import { cn } from '../lib/utils'

interface FreshnessMetaProps {
  meta: MetaView
  deployedAt: string
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

// The shell distinguishes the deployed UI build from when the serving data was generated.
// Store-specific as-of values, including market price dates, stay on their pages.
export function FreshnessMeta({ meta, deployedAt, className }: FreshnessMetaProps) {
  return (
    <div className={cn('flex items-center gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted-foreground', className)}>
      <Field label="デプロイ" value={formatJstStamp(deployedAt)} />
      <Field label="画面データ生成" value={formatJstStamp(meta.generated_at)} />
    </div>
  )
}
