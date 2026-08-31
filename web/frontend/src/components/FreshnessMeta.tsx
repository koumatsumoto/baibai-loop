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

// The shell distinguishes the deployed UI build from the newest store-backed data.
// Store-specific as-of values stay on the pages that consume those stores.
export function FreshnessMeta({ meta, deployedAt, className }: FreshnessMetaProps) {
  return (
    <div className={cn('flex items-center gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted-foreground', className)}>
      <Field label="デプロイ" value={formatJstStamp(deployedAt)} />
      <Field label="データ更新" value={meta.data_updated_at === null ? '—' : formatJstStamp(meta.data_updated_at)} />
    </div>
  )
}
