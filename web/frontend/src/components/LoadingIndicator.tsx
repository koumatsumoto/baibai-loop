import { DotPulse } from 'ldrs/react'
import 'ldrs/react/DotPulse.css'

// The layout route keeps the shell on screen, so this fills the space under it.
export function LoadingPage({ label }: { label: string }) {
  return (
    <main className="grid min-h-[60vh] place-items-center bg-background px-6">
      <span aria-label={label} className="inline-grid place-items-center text-primary" role="status">
        <DotPulse color="currentColor" size={44} speed={1.3} />
      </span>
    </main>
  )
}
