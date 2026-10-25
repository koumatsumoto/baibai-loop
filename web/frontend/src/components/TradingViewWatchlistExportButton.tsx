import { Download } from 'lucide-react'

import { tradingViewWatchlistText } from '../lib/trading-view'
import { Button } from './ui/button'

interface TradingViewWatchlistExportButtonProps {
  tickers: readonly string[]
  filename: string
  label: string
}

export function TradingViewWatchlistExportButton({ tickers, filename, label }: TradingViewWatchlistExportButtonProps) {
  function download() {
    const content = tradingViewWatchlistText(tickers)
    if (!content) return
    const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    try {
      link.click()
    } finally {
      link.remove()
      URL.revokeObjectURL(url)
    }
  }

  return <Button disabled={tickers.length === 0} onClick={download} size="sm" variant="outline"><Download aria-hidden="true" />{label}</Button>
}
