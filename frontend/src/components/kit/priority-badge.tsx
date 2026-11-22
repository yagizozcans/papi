import { cn } from '@/lib/utils'

export type Priority = 'P1' | 'P2' | 'P3'

export const priorityClass: Record<Priority, string> = {
  P1: 'border-p1/50 bg-p1/10 text-p1',
  P2: 'border-p2/50 bg-p2/10 text-p2',
  P3: 'border-p3/40 bg-transparent text-p3',
}

/** Disclosure priority: P1 red, P2 amber, P3 grey. Text label always present (not colour-only). */
export function PriorityBadge({ priority, className }: { priority: Priority; className?: string }) {
  return (
    <span
      data-priority={priority}
      className={cn(
        'inline-flex h-[18px] min-w-[26px] shrink-0 items-center justify-center rounded-sm border px-1 font-mono text-xs font-semibold leading-none',
        priorityClass[priority],
        className,
      )}
    >
      {priority}
    </span>
  )
}
