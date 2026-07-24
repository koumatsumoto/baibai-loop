import type { HTMLAttributes } from 'react'

import { cn } from '../lib/utils'

export function BrandMark({ className, ...props }: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      aria-hidden="true"
      className={cn('relative block size-8 shrink-0 overflow-hidden rounded-[0.625rem] bg-white ring-1 ring-border/80', className)}
      {...props}
    >
      <img
        alt=""
        className="absolute size-11 max-w-none -translate-x-1.5 -translate-y-1.5"
        height="44"
        src="/logo.png"
        width="44"
      />
    </span>
  )
}
