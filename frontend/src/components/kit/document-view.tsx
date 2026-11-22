import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'

const NUMERIC = /^[\s(+\-\u2212]*\d[\d.,\s]*%?\)?\s*$/

/** Finds an element by id inside `root` without relying on CSS.escape (absent in jsdom). */
function findById(root: HTMLElement, id: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`[id="${id.replace(/["\\]/g, '\\$&')}"]`)
}

/**
 * Reading view for TRUSTED, already-escaped server-generated HTML (KAP disclosure bodies).
 * Never pass user-supplied or unescaped HTML. Wide tables scroll horizontally, numeric cells are mono and
 * right-aligned. `highlightAnchor` scrolls to the element with that id and flashes it.
 */
export function DocumentView({
  html,
  highlightAnchor,
  className,
}: {
  html: string
  highlightAnchor?: string | null
  className?: string
}) {
  const ref = useRef<HTMLElement>(null)

  useEffect(() => {
    const root = ref.current
    if (!root) return
    root.querySelectorAll<HTMLElement>('td, th').forEach((cell) => {
      if (NUMERIC.test(cell.textContent ?? '')) cell.setAttribute('data-num', '')
    })
  }, [html])

  useEffect(() => {
    const root = ref.current
    if (!root || !highlightAnchor) return
    const el = findById(root, highlightAnchor)
    if (!el) return
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })
    el.classList.remove('doc-flash')
    void el.offsetWidth // restart the animation when the same anchor is highlighted again
    el.classList.add('doc-flash')
    const timer = window.setTimeout(() => el.classList.remove('doc-flash'), 2200)
    return () => {
      window.clearTimeout(timer)
      el.classList.remove('doc-flash')
    }
  }, [highlightAnchor, html])

  return (
    <article
      ref={ref}
      lang="tr"
      className={cn('doc mx-auto max-w-[78ch]', className)}
      // Trusted server-generated, escaped HTML; see the component docs.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
