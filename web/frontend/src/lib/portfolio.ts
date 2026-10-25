import type { HoldingView } from '../api/types'

export interface UnrealizedPnlTotal {
  yen: number | null
  pct: number | null
}

// Monetary gains use gold and losses use blue. Labels and signs carry the same meaning.
export function pnlTone(value: number | null): string {
  if (value === null || value === 0) return 'text-muted-foreground'
  return value > 0 ? 'text-profit' : 'text-loss'
}

export function totalUnrealizedPnl(holdings: readonly HoldingView[]): UnrealizedPnlTotal {
  if (holdings.some((holding) => holding.unrealized_pnl_yen === null)) return { yen: null, pct: null }
  const yen = holdings.reduce((total, holding) => total + (holding.unrealized_pnl_yen ?? 0), 0)
  const deployedCostYen = holdings.reduce((total, holding) => total + holding.deployed_cost_yen, 0)
  return {
    yen,
    pct: deployedCostYen > 0 ? yen / deployedCostYen * 100 : null,
  }
}
