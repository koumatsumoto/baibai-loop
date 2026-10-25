export function tradingViewChartUrl(ticker: string) {
  return tradingViewSymbolChartUrl(tradingViewTseSymbol(ticker))
}

export function tradingViewTseSymbol(ticker: string) {
  return `TSE:${ticker}`
}

export function tradingViewWatchlistText(tickers: readonly string[]) {
  return [...new Set(tickers)].map(tradingViewTseSymbol).join(',')
}

export function tradingViewSymbolChartUrl(rawSymbol: string) {
  const symbol = encodeURIComponent(rawSymbol)
  return `https://www.tradingview.com/chart/?symbol=${symbol}`
}
