import type * as React from 'react'
import { cn } from '@/lib/utils'

function Kbd({ className, ...props }: React.ComponentProps<'kbd'>) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        'inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-sm border border-border bg-muted px-1 font-mono text-xs leading-none text-muted-foreground',
        className,
      )}
      {...props}
    />
  )
}

export { Kbd }
