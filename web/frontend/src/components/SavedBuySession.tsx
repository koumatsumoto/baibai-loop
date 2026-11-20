import type { SavedProposalView } from '../api/types'

export function SavedBuySession({ item }: { item: SavedProposalView['items'][number] }) {
  if (item.action !== 'buy' || !item.terms?.target_session || !item.terms.limit_price_yen) return null
  const { limit_price_yen, target_session } = item.terms
  return <span className="min-w-0 break-words text-xs leading-5 text-muted-foreground">
    対象取引日 <time dateTime={target_session}>{target_session}</time> の保存指値 <span className="tabular-nums">{limit_price_yen} 円</span>
  </span>
}
