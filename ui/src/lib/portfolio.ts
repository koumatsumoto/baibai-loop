import type { HoldingView } from '../api/types'

export interface UnrealizedPnlTotal {
  yen: number
  pct: number | null
}

export function totalUnrealizedPnl(holdings: readonly HoldingView[]): UnrealizedPnlTotal {
  const yen = holdings.reduce((total, holding) => total + holding.unrealized_pnl_yen, 0)
  const deployedCostYen = holdings.reduce((total, holding) => total + holding.deployed_cost_yen, 0)
  return {
    yen,
    pct: deployedCostYen > 0 ? yen / deployedCostYen * 100 : null,
  }
}
