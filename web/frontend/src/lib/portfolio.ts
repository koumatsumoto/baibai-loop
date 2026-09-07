import type { HoldingView } from '../api/types'

export interface UnrealizedPnlTotal {
  yen: number | null
  pct: number | null
}

// Money outcomes wear gold when they gained and blue when they lost, apart from the
// green/red every other number uses for direction. One holding down 3% and one screening
// column down 3% ask for different decisions, so they are never the same color.
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
