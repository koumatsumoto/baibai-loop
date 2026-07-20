import { Link } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'

import { AppShell } from './AppShell'
import { Button } from './ui/button'
import { cn } from '../lib/utils'

interface PageStateProps {
  message: string
  // Small muted line above the message (page name, ticker, error kind).
  title?: string
  // Render the title in a monospace face (used for tickers).
  mono?: boolean
  // Offer a link back to Screening (unknown-security state).
  back?: boolean
}

export function PageState({ message, title, mono = false, back = false }: PageStateProps) {
  return (
    <>
      <AppShell />
      <main className="mx-auto grid min-h-[60vh] max-w-5xl place-items-center px-6 text-center">
        <div>
          {title !== undefined && (
            <p className={cn('text-sm font-medium text-muted-foreground', mono && 'font-mono')}>{title}</p>
          )}
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">{message}</h1>
          {back && (
            <Button asChild className="mt-6" variant="outline">
              <Link to="/screening"><ArrowLeft />Screening に戻る</Link>
            </Button>
          )}
        </div>
      </main>
    </>
  )
}
