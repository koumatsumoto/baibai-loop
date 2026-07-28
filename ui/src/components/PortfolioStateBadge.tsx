import type { PortfolioState } from '../api/types'
import { Badge } from './ui/badge'

const PORTFOLIO_STATE_LABEL: Record<PortfolioState, string | null> = {
  unheld: null,
  held: '保有',
  reserved: '予約',
  held_and_reserved: '保有+予約',
}

export function PortfolioStateBadge({ state }: { state: PortfolioState | null }) {
  const label = state === null ? null : PORTFOLIO_STATE_LABEL[state]
  if (label === null) return <span className="text-muted-foreground">—</span>
  return <Badge variant={state === 'reserved' ? 'outline' : 'secondary'}>{label}</Badge>
}
