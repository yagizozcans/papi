import type { ReactNode } from 'react'
import { ErrorState } from '@/components/ui/error-state'
import { Skeleton } from '@/components/ui/skeleton'

export function errorText(error: unknown): string | undefined {
  return error instanceof Error ? error.message : undefined
}

/** Panel-sized error with retry. */
export function PanelError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return <ErrorState description={errorText(error)} onRetry={onRetry} className="py-6" />
}

export function SkeletonLines({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={className ?? 'flex flex-col gap-2 p-3'} aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-3.5" style={{ width: `${88 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  )
}

/** Renders skeleton / error / children for a TanStack query. */
export function QueryBody({
  loading,
  error,
  onRetry,
  rows,
  children,
}: {
  loading: boolean
  error: unknown
  onRetry?: () => void
  rows?: number
  children: ReactNode
}) {
  if (loading) return <SkeletonLines rows={rows} />
  if (error) return <PanelError error={error} onRetry={onRetry} />
  return <>{children}</>
}
