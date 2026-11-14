import type { SavedAssessmentSourceView } from '../api/types'
import { formatJstDateTime } from '../lib/format'

export function AssessmentSources({ sourceIds, sources }: { sourceIds: string[]; sources: SavedAssessmentSourceView[] }) {
  if (sourceIds.length === 0) return null
  return <details className="mt-2">
    <summary className="cursor-pointer text-xs text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-4">保存根拠を確認</summary>
    <ul className="mt-2 grid gap-3 text-xs leading-6">
      {sourceIds.map((id, index) => {
        const source = sources.find(item => item.source_id === id)
        return <li className="min-w-0 break-words rounded-md bg-muted p-3" key={`${id}-${index}`}>
          <p className="break-all font-medium">{id}{source !== undefined && ` · ${{ primary: '一次資料', secondary: '二次資料', local_data: '保存データ' }[source.source_tier]}`}</p>
          {source === undefined ? <p>参照先の表示情報なし</p> : <>
            {source.ref !== null ? <a className="break-all underline" href={source.ref} target="_blank" rel="noopener noreferrer">{source.ref}</a> : <p className="break-all">保存データ：{source.provider} / {source.dataset}</p>}
            <p>{source.used_for}</p>
            <p>根拠の対象日：<time dateTime={source.as_of}>{source.as_of}</time></p>
            <p>取得時刻：<time dateTime={source.retrieved_at}>{formatJstDateTime(source.retrieved_at)} JST</time></p>
          </>}
        </li>
      })}
    </ul>
  </details>
}
