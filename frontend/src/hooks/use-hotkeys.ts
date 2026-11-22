import { useNavigate } from '@tanstack/react-router'
import { useEffect } from 'react'
import { NAV } from '@/lib/nav'

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

interface Options {
  togglePalette: () => void
  openShortcuts: () => void
}

/** Global keyboard shortcuts: Ctrl/Cmd+K, `?`, and `g` followed by a page letter. */
export function useHotkeys({ togglePalette, openShortcuts }: Options) {
  const navigate = useNavigate()

  useEffect(() => {
    let pendingG: number | undefined

    const clear = () => {
      window.clearTimeout(pendingG)
      pendingG = undefined
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        togglePalette()
        return
      }
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return

      if (pendingG !== undefined) {
        clear()
        const item = NAV.find((n) => n.hotkey === e.key.toLowerCase())
        if (item) {
          e.preventDefault()
          void navigate({ to: item.to })
        }
        return
      }
      if (e.key === 'g') {
        pendingG = window.setTimeout(clear, 1200)
      } else if (e.key === '?') {
        e.preventDefault()
        openShortcuts()
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      clear()
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [navigate, togglePalette, openShortcuts])
}
