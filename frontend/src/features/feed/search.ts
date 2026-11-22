import type { Priority } from '@/components/kit/priority-badge'

export const SCOPES = ['mine', 'holdings', 'watchlist', 'all'] as const
export const BANDS = ['low', 'medium', 'high', 'critical'] as const
export const SORTS = ['time_desc', 'time_asc', 'materiality'] as const
export const RANGES = ['today', 'yesterday', '7d'] as const

export type Scope = (typeof SCOPES)[number]
export type Band = (typeof BANDS)[number]
export type Sort = (typeof SORTS)[number]
export type Range = (typeof RANGES)[number]

export const DEFAULT_SCOPE: Scope = 'mine'
export const DEFAULT_SORT: Sort = 'time_desc'
export const PRIORITIES: readonly Priority[] = ['P1', 'P2', 'P3']

/** Feed filters as router search params. Defaults are omitted from the URL (undefined). */
export interface FeedSearch {
  scope?: Scope
  /** Comma-separated subset of P1,P2,P3 (e.g. "P1,P2"). */
  priority?: string
  range?: Range
  band?: Band
  ticker?: string
  family?: string
  code?: string
  sort?: Sort
  q?: string
  /** Hide routine disclosures. */
  routine?: true
}

function str(v: unknown): string | undefined {
  if (typeof v === 'number') return String(v)
  return typeof v === 'string' && v.trim() ? v.trim() : undefined
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[]): T | undefined {
  const s = str(v)
  return allowed.find((a) => a === s)
}

export function parsePriorities(value: string | undefined): Priority[] {
  const set = new Set((value ?? '').toUpperCase().split(','))
  return PRIORITIES.filter((p) => set.has(p))
}

/** `validateSearch` for the /feed routes: drops unknown or invalid values. */
export function validateFeedSearch(raw: Record<string, unknown>): FeedSearch {
  const scope = oneOf(raw.scope, SCOPES)
  const sort = oneOf(raw.sort, SORTS)
  const priority = parsePriorities(str(raw.priority)).join(',')
  const ticker = str(raw.ticker)?.toUpperCase()
  return {
    scope: scope && scope !== DEFAULT_SCOPE ? scope : undefined,
    priority: priority || undefined,
    range: oneOf(raw.range, RANGES),
    band: oneOf(raw.band, BANDS),
    ticker,
    family: str(raw.family),
    code: str(raw.code),
    sort: sort && sort !== DEFAULT_SORT ? sort : undefined,
    q: str(raw.q),
    routine: raw.routine === true || raw.routine === 'true' || raw.routine === 1 ? true : undefined,
  }
}

/** Number of active filters other than scope and sort (drives the "empty because of filters" hint). */
export function hasActiveFilters(s: FeedSearch): boolean {
  return !!(s.priority || s.range || s.band || s.ticker || s.family || s.code || s.q || s.routine)
}

/** Query params for GET /v1/feed. The API takes one priority threshold ("this or more urgent"). */
export function feedQueryParams(s: FeedSearch) {
  const selected = parsePriorities(s.priority)
  return {
    scope: s.scope ?? DEFAULT_SCOPE,
    sort: s.sort ?? DEFAULT_SORT,
    priority: selected.length ? selected[selected.length - 1] : undefined,
    range: s.range,
    band: s.band,
    ticker: s.ticker,
    family: s.family,
    code: s.code,
    q: s.q,
    hide_routine: s.routine,
  }
}
