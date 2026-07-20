export function tradingViewChartUrl(ticker: string) {
  return tradingViewSymbolChartUrl(`TSE:${ticker}`)
}

export function tradingViewSymbolChartUrl(rawSymbol: string) {
  const symbol = encodeURIComponent(rawSymbol)
  return `https://www.tradingview.com/chart/?symbol=${symbol}`
}
