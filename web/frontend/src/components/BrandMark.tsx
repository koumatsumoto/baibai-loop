import type { ImgHTMLAttributes } from 'react'

import { cn } from '../lib/utils'

// The mark is drawn as-is: a centered square image with nothing shaped around one
// particular logo, so replacing web/frontend/brand/logo.png and regenerating the assets is the
// whole of a rebrand here.
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
