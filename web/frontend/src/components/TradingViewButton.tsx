import { ChartNoAxesCombined } from 'lucide-react'

import { Button } from './ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip'
import { tradingViewChartUrl } from '../lib/trading-view'

interface TradingViewButtonProps {
  ticker: string
}

export function TradingViewButton({ ticker }: TradingViewButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild size="icon-sm" variant="ghost">
          <a
            aria-label={`${ticker} の TradingView チャートを開く`}
            href={tradingViewChartUrl(ticker)}
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
