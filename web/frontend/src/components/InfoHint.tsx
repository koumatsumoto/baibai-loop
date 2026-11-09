import { useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { Info } from 'lucide-react'

import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import { cn } from '../lib/utils'

interface InfoHintProps {
  label: string
  children: ReactNode
  className?: string
}

// Mouse uses hover; touch and keyboard use the same trigger to toggle.
export function InfoHint({ label, children, className }: InfoHintProps) {
  const [open, setOpen] = useState(false)
  const pointerType = useRef('')
  const onHover = (next: boolean) => (event: PointerEvent) => {
    pointerType.current = event.pointerType
    if (event.pointerType === 'mouse') setOpen(next)
  }
  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger
        aria-label={`${label}の説明`}
        className={cn(
          'inline-flex size-4 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none',
          className,
        )}
        onClick={(event) => {
          // Keep hover open on mouse click; keyboard activation must still toggle.
          if (event.detail !== 0 && pointerType.current === 'mouse') event.preventDefault()
        }}
        onPointerEnter={onHover(true)}
        onPointerLeave={onHover(false)}
        type="button"
      >
        <Info aria-hidden="true" className="size-3.5" />
      </PopoverTrigger>
      <PopoverContent
        className="text-xs leading-relaxed font-normal text-foreground"
        // Text-only content does not need to take focus.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onPointerEnter={onHover(true)}
        onPointerLeave={onHover(false)}
      >
        {children}
      </PopoverContent>
    </Popover>
  )
}
