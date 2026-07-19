export function tradingViewChartUrl(ticker: string) {
  const symbol = encodeURIComponent(`TSE:${ticker}`)
  return `https://www.tradingview.com/chart/?symbol=${symbol}`
}
