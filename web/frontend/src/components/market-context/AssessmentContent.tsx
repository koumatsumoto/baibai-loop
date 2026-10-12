import type { AssessmentClaimView, AssessmentContentView } from '../../api/types'

function Claims({ title, items }: { title: string; items: AssessmentClaimView[] }) {
  if (!items.length) return null
  return <section className="grid gap-3"><h3 className="border-b pb-2 font-semibold">{title}</h3>{items.map((item, index) => <article key={index} className="grid gap-1"><p className="whitespace-pre-wrap">{item.summary}</p>{item.source_ids.length > 0 && <p className="text-xs text-muted-foreground">出典: {item.source_ids.join(' / ')}</p>}</article>)}</section>
}

export function AssessmentContent({ data }: { data: AssessmentContentView }) {
  return <div className="grid gap-6">
    <p className="text-xs text-muted-foreground">入力確定時刻: {data.known_at}</p>
    <Claims title="重要な変化と根拠" items={data.changes} />
    <Claims title="反証となる材料" items={data.counter_evidence} />
    <Claims title="不確実性・不足" items={data.uncertainties} />
    <Claims title="調査・リスクへの含意" items={data.implications} />
    {data.inputs.length > 0 && <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">固定した入力と出典</summary><ul className="mt-3 grid gap-3">{data.inputs.map((input, index) => <li key={index} className="text-sm"><p>{String(input.input_id)} · {String(input.status)}</p><p className="break-all text-muted-foreground">{String(input.reference)}</p><p className="text-xs text-muted-foreground">利用可能時刻: {String(input.available_at)}</p></li>)}</ul></details>}
  </div>
}
