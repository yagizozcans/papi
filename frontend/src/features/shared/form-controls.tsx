import type * as React from 'react'
import { cn } from '@/lib/utils'

export const controlClass =
  'h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring disabled:opacity-50 aria-[invalid=true]:border-error'

export function Input({ className, ...props }: React.ComponentProps<'input'>) {
  return <input className={cn(controlClass, className)} {...props} />
}

export function Select({ className, ...props }: React.ComponentProps<'select'>) {
  return <select className={cn(controlClass, 'bg-popover', className)} {...props} />
}

/** Label + control + inline error. */
export function Field({
  label,
  htmlFor,
  error,
  hint,
  className,
  children,
}: {
  label: string
  htmlFor: string
  error?: string
  hint?: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={htmlFor} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
      {error ? (
        <p role="alert" className="text-xs text-error">
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  )
}
