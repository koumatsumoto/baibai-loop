export function tradingViewChartUrl(ticker: string) {
  const symbol = encodeURIComponent(tradingViewTseSymbol(ticker))
  return `https://www.tradingview.com/chart/?symbol=${symbol}`
}

function tradingViewTseSymbol(ticker: string) {
  return `TSE:${ticker}`
}

export function tradingViewWatchlistText(tickers: readonly string[]) {
  return [...new Set(tickers)].map(tradingViewTseSymbol).join(',')
}
