import { useSyncExternalStore } from 'react'
import { readStorage, writeStorage } from './storage'

export const CB_KEY = 'papi-cb'

const listeners = new Set<() => void>()

/** Colour-blind-safe mode: up/down use blue/orange instead of green/red (see `html[data-cb]` in index.css). */
export function getColorBlind(): boolean {
  return readStorage(CB_KEY) === '1'
}

export function applyColorBlind(on: boolean): void {
  if (on) document.documentElement.setAttribute('data-cb', '1')
  else document.documentElement.removeAttribute('data-cb')
}

export function setColorBlind(on: boolean): void {
  writeStorage(CB_KEY, on ? '1' : '0')
  applyColorBlind(on)
  listeners.forEach((l) => l())
}

export function toggleColorBlind(): void {
  setColorBlind(!getColorBlind())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function useColorBlind(): boolean {
  return useSyncExternalStore(subscribe, getColorBlind, () => false)
}
