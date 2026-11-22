import { useState } from 'react'

/**
 * Holds back items that arrive after the first load so a live list does not jump under the cursor.
 * `visible` contains the items the user has already seen; `pending` counts the new ones until `reveal()`.
 * Call with `ready = false` while the first fetch is in flight. Remount (key) to reset.
 */
export function useHeldItems<T>(items: readonly T[], getId: (item: T) => string, ready = true) {
  const [seen, setSeen] = useState<ReadonlySet<string> | null>(null)
  const ids = items.map(getId)

  if (ready && seen === null) setSeen(new Set(ids))

  const known = seen ?? new Set(ids)
  const visible = items.filter((item) => known.has(getId(item)))
  const pending = items.length - visible.length

  return { visible, pending, reveal: () => setSeen(new Set(ids)) }
}
