import { DotPulse } from 'ldrs/react'
import 'ldrs/react/DotPulse.css'

import { cn } from '../lib/utils'

interface LoadingIndicatorProps {
  label: string
  className?: string
  size?: number
}

export function LoadingIndicator({ label, className, size = 36 }: LoadingIndicatorProps) {
  return (
    <span
      aria-label={label}
      className={cn('inline-grid place-items-center text-primary', className)}
      role="status"
    >
      <DotPulse color="currentColor" size={size} speed={1.3} />
    </span>
  )
}

// The layout route keeps the shell on screen, so this fills the space under it.
export function LoadingPage({ label }: { label: string }) {
  return (
    <main className="grid min-h-[60vh] place-items-center bg-background px-6">
      <LoadingIndicator label={label} size={44} />
    </main>
  )
}
