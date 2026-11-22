import type { JsonValue, SavedMarketContextView } from '../api/types'

function object(value: JsonValue | undefined): Record<string, JsonValue> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, JsonValue> : null
}

function QuotedValue({ value }: { value: JsonValue }) {
  if (typeof value === 'string') return <p className="whitespace-pre-wrap break-words">{value}</p>
  const claim = object(value)
  if (claim !== null && typeof claim.summary === 'string') return <>
    <p className="whitespace-pre-wrap break-words">{claim.summary}</p>
    {Array.isArray(claim.source_ids) && <p className="text-xs break-all text-muted-foreground">出典 ID：{claim.source_ids.map(id => typeof id === 'string' ? id : JSON.stringify(id)).join(' / ') || '記録なし'}</p>}
  </>
  return <pre className="whitespace-pre-wrap break-words text-xs">{JSON.stringify(value, null, 2)}</pre>
}

function Section({ title, value }: { title: string; value: JsonValue | undefined }) {
  return <div className="grid gap-2">
    <p className="text-sm font-medium">{title}</p>
    {value == null ? <p className="text-sm text-muted-foreground">保存投影に記録なし</p>
      : Array.isArray(value) ? value.length === 0 ? <p className="text-sm text-muted-foreground">保存された項目は0件（影響なしの認定ではありません）</p>
        : <ul className="grid gap-3">{value.map((item, index) => <li key={index}><QuotedValue value={item} /></li>)}</ul>
        : <QuotedValue value={value} />}
  </div>
}

export function SavedMarketContext({ context }: { context: SavedMarketContextView | null | undefined }) {
  const assessment = context?.status === 'retained' ? context.assessment : null
  return <section className="grid gap-3 rounded-md border p-4" aria-label="提案開始時に固定した市場判断">
    <h3 className="font-medium">提案開始時に固定した市場判断</h3>
    <p className="text-xs leading-6 text-muted-foreground">同じ調査対象選定に保存された当時の判断です。現在の市場、個社価値の採択や売買推奨ではありません。後発の市場判断や現在の時計で補っていません。</p>
    {assessment == null ? <p className="text-sm text-muted-foreground">
      {context?.status === 'not_recorded' ? 'この提案の選定時点では市場判断が保存されていません。'
        : context?.status === 'unavailable' ? '元の選定と市場判断の対応を読み取れません。保存提案の採否は変更していません。'
          : 'この表示データには固定市場判断がありません。'}
      市場が中立・安全という意味ではありません。
    </p> : <>
      <QuotedValue value={assessment.summary ?? null} />
      <dl className="grid gap-2 text-xs sm:grid-cols-2">
        {([['as_of', '市場判断の対象日'], ['known_at', '市場判断が扱った情報の締切'], ['published_at', '市場判断の発行時刻']] as const).map(([key, label]) => <div key={key}><dt className="text-muted-foreground">{label}</dt><dd className="break-all">{typeof assessment[key] === 'string' ? <time dateTime={assessment[key]}>{assessment[key]}</time> : '未記録（他の時刻から推定しません）'}</dd></div>)}
        <div><dt className="text-muted-foreground">選定時に記録された鮮度状態</dt><dd>{typeof assessment.status === 'string' ? assessment.status : '未記録'}（現在の鮮度判定ではありません）</dd></div>
      </dl>
      <details>
        <summary className="cursor-pointer text-sm font-medium">変化・反証・未確認と個社への伝達</summary>
        <div className="mt-3 grid gap-4">
          <Section title="保存された変化" value={assessment.changes} />
          <Section title="市場判断への反証" value={assessment.counter_evidence} />
          <Section title="未確認・不確実性" value={assessment.uncertainties} />
          <Section title="個社調査・待機との接続" value={assessment.implications} />
          <details><summary className="cursor-pointer text-sm">根拠資料と当時の状態（保存値）</summary><pre className="mt-2 whitespace-pre-wrap break-words text-xs">{JSON.stringify(assessment.sources ?? null, null, 2)}</pre></details>
          <details><summary className="cursor-pointer text-xs text-muted-foreground">提案時に保存した市場判断データの全項目</summary><pre className="mt-2 whitespace-pre-wrap break-words text-xs">{JSON.stringify(assessment, null, 2)}</pre></details>
        </div>
      </details>
    </>}
    {context !== null && context !== undefined && <div className="text-xs text-muted-foreground break-all">
      <p>元 Selection SHA-256：{context.selection_sha256}</p>
      {assessment !== null && typeof assessment.context_id === 'string' && <p>市場判断 ID：{assessment.context_id}</p>}
      {context.reason !== null && <p>読取状態：{context.reason}</p>}
    </div>}
  </section>
}
