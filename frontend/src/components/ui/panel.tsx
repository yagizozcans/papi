import type * as React from 'react'
import { cn } from '@/lib/utils'

/** Bordered card with an optional dense header (title left, actions right). */
function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  ...props
}: Omit<React.ComponentProps<'section'>, 'title'> & {
  title?: React.ReactNode
  actions?: React.ReactNode
  bodyClassName?: string
}) {
  return (
    <section data-slot="panel" className={cn('flex min-w-0 flex-col overflow-hidden rounded-md border border-border bg-card', className)} {...props}>
      {(title || actions) && (
        <header className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border px-3">
          <h2 className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</h2>
          {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
        </header>
      )}
      <div className={cn('min-h-0 flex-1', bodyClassName)}>{children}</div>
    </section>
  )
}

/** Borderless page section: heading row plus content. */
function Section({
  title,
  actions,
  children,
  className,
  ...props
}: Omit<React.ComponentProps<'section'>, 'title'> & { title?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section data-slot="section" className={cn('flex min-w-0 flex-col gap-2', className)} {...props}>
      {(title || actions) && (
        <header className="flex min-h-7 items-center justify-between gap-2">
          <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
          {actions && <div className="flex items-center gap-1">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  )
}

export { Panel, Section }
