import type { Priority } from '@/components/kit/priority-badge'

/** API priorities run P1..P4; the badge and chart know P1..P3 (P4 shares the muted P3 style). */
export function toPriority(p: string | null | undefined): Priority | null {
  if (p === 'P1' || p === 'P2') return p
  return p ? 'P3' : null
}
