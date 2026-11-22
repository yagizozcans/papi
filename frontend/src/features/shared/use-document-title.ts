import { useEffect } from 'react'

/** Sets `document.title` to "<title> · papi" while the calling page is mounted. */
export function useDocumentTitle(title: string | null | undefined) {
  useEffect(() => {
    if (!title) return
    const previous = document.title
    document.title = `${title} · papi`
    return () => {
      document.title = previous
    }
  }, [title])
}
