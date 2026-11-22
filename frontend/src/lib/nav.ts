import { Activity, FlaskConical, Newspaper, Sun, Telescope, Wallet, type LucideIcon } from 'lucide-react'

export type NavTo = '/' | '/feed' | '/portfolio' | '/research' | '/lab' | '/system'

export interface NavItem {
  key: 'today' | 'feed' | 'portfolio' | 'research' | 'lab' | 'system'
  to: NavTo
  icon: LucideIcon
  /** Second key of the `g <key>` shortcut. */
  hotkey: string
  soon?: boolean
}

export const NAV: readonly NavItem[] = [
  { key: 'today', to: '/', icon: Sun, hotkey: 't' },
  { key: 'feed', to: '/feed', icon: Newspaper, hotkey: 'f' },
  { key: 'portfolio', to: '/portfolio', icon: Wallet, hotkey: 'p' },
  { key: 'research', to: '/research', icon: Telescope, hotkey: 'r' },
  { key: 'lab', to: '/lab', icon: FlaskConical, hotkey: 'l', soon: true },
  { key: 'system', to: '/system', icon: Activity, hotkey: 's' },
]

/** Pages shown directly in the mobile tab bar; the rest go under "More". */
export const MOBILE_TABS: readonly NavItem['key'][] = ['today', 'feed', 'portfolio', 'research']
