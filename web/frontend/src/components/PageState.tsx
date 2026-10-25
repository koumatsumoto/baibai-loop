import { cn } from '../lib/utils'

interface PageStateProps {
  message: string
  // Small muted line above the message (page name, ticker, error kind).
  title?: string
  // Render the title in a monospace face (used for tickers).
  mono?: boolean
}

export function PageState({ message, title, mono = false }: PageStateProps) {
  return (
    <main className="mx-auto grid min-h-[60vh] max-w-5xl place-items-center px-6 text-center">
      <div>
        {title !== undefined && (
          <p className={cn('text-sm font-medium text-muted-foreground', mono && 'font-mono')}>{title}</p>
        )}
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{message}</h1>
      </div>
    </main>
  )
}
