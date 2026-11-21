export interface UnrealizedPnlTotal {
  yen: number | null
  pct: number | null
}

// Monetary gains use gold and losses use blue. Labels and signs carry the same meaning.
export function pnlTone(value: number | null): string {
  if (value === null || value === 0) return 'text-muted-foreground'
  return value > 0 ? 'text-profit' : 'text-loss'
}

export function totalUnrealizedPnl(marketValueYen: number | null, deployedCostYen: number | null): UnrealizedPnlTotal {
  if (marketValueYen === null || deployedCostYen === null) return { yen: null, pct: null }
  // The server rounds the exact aggregate independently of the displayed holding rows.
  const yen = marketValueYen - deployedCostYen
  return {
    yen,
    pct: deployedCostYen > 0 ? yen / deployedCostYen * 100 : null,
  }
}
