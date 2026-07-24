import { DotPulse } from 'ldrs/react'
import 'ldrs/react/DotPulse.css'

import { AppShell } from './AppShell'
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

export function LoadingPage({ label, shell = true }: { label: string; shell?: boolean }) {
  const content = (
    <main className={cn('grid place-items-center bg-background px-6', shell ? 'min-h-[60vh]' : 'min-h-screen')}>
      <LoadingIndicator label={label} size={44} />
    </main>
  )

  if (!shell) return content
  return <><AppShell />{content}</>
}
