import { ChartNoAxesCombined } from 'lucide-react'

import { Button } from './ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip'
import { tradingViewChartUrl, tradingViewSymbolChartUrl } from '../lib/trading-view'

interface TradingViewButtonProps {
  // Exactly one of ticker (TSE:<ticker>) or symbol (raw TradingView symbol) is given.
  ticker?: string
  symbol?: string
  // Accessible-name context; defaults to the ticker / symbol.
  name?: string
  // Render a labelled outline button instead of the compact icon + tooltip.
  labeled?: boolean
}

export function TradingViewButton({ ticker, symbol, name, labeled = false }: TradingViewButtonProps) {
  const href = ticker !== undefined ? tradingViewChartUrl(ticker) : tradingViewSymbolChartUrl(symbol ?? '')
  const accessibleName = name ?? ticker ?? symbol ?? ''

  if (labeled) {
    return (
      <Button asChild variant="outline">
        <a href={href} rel="noopener noreferrer" target="_blank">
          <ChartNoAxesCombined aria-hidden="true" />TradingView
        </a>
      </Button>
    )
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild size="icon-sm" variant="ghost">
          <a
            aria-label={`${accessibleName} の TradingView チャートを開く`}
            href={href}
            rel="noopener noreferrer"
            target="_blank"
          >
            <ChartNoAxesCombined aria-hidden="true" />
          </a>
        </Button>
      </TooltipTrigger>
      <TooltipContent>TradingView でチャートを開く</TooltipContent>
    </Tooltip>
  )
}
