import { useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { Info } from 'lucide-react'

import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import { cn } from '../lib/utils'

interface InfoHintProps {
  // What the hint explains. Names the trigger for assistive technology, so it reads as
  // "percentile の説明" rather than an unlabelled icon among a hundred others.
  label: string
  children: ReactNode
  className?: string
}

// A term that needs a sentence to be read correctly carries it next to its own label,
// instead of in a paragraph above the table: the explanation is one gesture away and
// never sits between the reader and the numbers.
//
// A mouse opens it on hover so a glance suffices. Touch has no hover at all, so the same
// trigger opens on tap — the hint is unreadable on a phone otherwise. Pointer type, not a
// media query, decides which applies, so a hybrid device gets both.
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
          // Hover already governs the mouse, and letting the click through would close
          // the hint the moment a reader clicks what looks like a button. A keyboard
          // activation carries no pointer (detail 0) and must still toggle.
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
        // The content is text, so moving focus into it would only strand the keyboard
        // user; Escape still closes because the dismiss layer listens on the document.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onPointerEnter={onHover(true)}
        onPointerLeave={onHover(false)}
      >
        {children}
      </PopoverContent>
    </Popover>
  )
}
