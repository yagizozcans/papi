import { useSyncExternalStore } from 'react'
import { readStorage, writeStorage } from './storage'

export type Theme = 'dark' | 'light'
export const THEME_KEY = 'papi-theme'

const listeners = new Set<() => void>()

/** Dark unless the user explicitly chose light. The OS preference is ignored on purpose. */
export function getTheme(): Theme {
  return readStorage(THEME_KEY) === 'light' ? 'light' : 'dark'
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

export function setTheme(theme: Theme): void {
  writeStorage(THEME_KEY, theme)
  applyTheme(theme)
  listeners.forEach((l) => l())
}

export function toggleTheme(): void {
  setTheme(getTheme() === 'dark' ? 'light' : 'dark')
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, () => 'dark' as Theme)
}
