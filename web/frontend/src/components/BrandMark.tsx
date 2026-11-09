import type { ImgHTMLAttributes } from 'react'

import { cn } from '../lib/utils'

export function BrandMark({ className, ...props }: ImgHTMLAttributes<HTMLImageElement>) {
  return (
    <img
      alt=""
      aria-hidden="true"
      className={cn('block size-8 shrink-0', className)}
      height="32"
      src="/logo.png"
      width="32"
      {...props}
    />
  )
}
