import i18n from '@/i18n'
import { TIME_ZONE } from '@/lib/format'
import type { components } from '@/api/schema'

export type FeedItem = components['schemas']['FeedItem']

const hm = new Intl.DateTimeFormat('tr-TR', { timeZone: TIME_ZONE, hour: '2-digit', minute: '2-digit', hour12: false })
const dm = new Intl.DateTimeFormat('tr-TR', { timeZone: TIME_ZONE, day: '2-digit', month: '2-digit' })

/** HH:MM in Europe/Istanbul. */
export function formatHM(iso: string | null | undefined): string {
  const d = iso ? new Date(iso) : null
  return d && !Number.isNaN(d.getTime()) ? hm.format(d) : '–'
}

/** DD.MM in Europe/Istanbul. */
export function formatDM(iso: string | null | undefined): string {
  const d = iso ? new Date(iso) : null
  return d && !Number.isNaN(d.getTime()) ? dm.format(d) : ''
}

export function headlineOf(item: Pick<FeedItem, 'headline' | 'headline_en'>): string {
  return i18n.language === 'en' && item.headline_en ? item.headline_en : item.headline
}

export function sortKey(item: FeedItem): string {
  return `${item.published_key}\u0000${item.id}`
}

/** Merges item lists, de-duplicating by id (first occurrence wins). */
export function dedupe(...lists: readonly (readonly FeedItem[])[]): FeedItem[] {
  const seen = new Set<string>()
  const out: FeedItem[] = []
  for (const list of lists) {
    for (const it of list) {
      if (seen.has(it.id)) continue
      seen.add(it.id)
      out.push(it)
    }
  }
  return out
}

/* ---- burst grouping ---- */

export const BURST_MIN = 4
export const BURST_WINDOW_MS = 10 * 60 * 1000

export type TapeRow =
  | { type: 'item'; key: string; item: FeedItem; nested: boolean }
  | { type: 'burst'; key: string; items: FeedItem[]; open: boolean }

/** Collapses runs of >= BURST_MIN consecutive same-code items within BURST_WINDOW_MS into one burst row. */
export function buildRows(items: readonly FeedItem[], grouping: boolean, expanded: ReadonlySet<string>, selectedId?: string | null): TapeRow[] {
  const rows: TapeRow[] = []
  let i = 0
  while (i < items.length) {
    const first = items[i]!
    let j = i + 1
    if (grouping) {
      const t0 = Date.parse(first.announced_at)
      while (j < items.length && items[j]!.code === first.code && Math.abs(Date.parse(items[j]!.announced_at) - t0) <= BURST_WINDOW_MS) j++
    }
    if (grouping && j - i >= BURST_MIN) {
      const group = items.slice(i, j)
      const key = `burst:${first.id}`
      const open = expanded.has(key) || group.some((g) => g.id === selectedId)
      rows.push({ type: 'burst', key, items: group, open })
      if (open) for (const it of group) rows.push({ type: 'item', key: it.id, item: it, nested: true })
    } else {
      j = i + 1
      rows.push({ type: 'item', key: first.id, item: first, nested: false })
    }
    i = j
  }
  return rows
}

/* ---- value formatting ---- */

export interface KV {
  path: string
  value: string
}

function scalar(v: unknown): string {
  if (v == null) return '–'
  if (typeof v === 'number') return Number.isFinite(v) ? v.toLocaleString(i18n.language === 'en' ? 'en-US' : 'tr-TR', { maximumFractionDigits: 6 }) : '–'
  if (typeof v === 'boolean') return i18n.t(v ? 'feed.yes' : 'feed.no')
  return String(v)
}

const isScalar = (v: unknown) => v == null || ['string', 'number', 'boolean'].includes(typeof v)

/** Display string for a free-form value (scalars, lists of scalars; anything else as JSON). */
export function formatValue(v: unknown): string {
  if (isScalar(v)) return scalar(v)
  if (Array.isArray(v) && v.every(isScalar)) return v.map(scalar).join(', ')
  return JSON.stringify(v)
}

/** Flattens nested objects/arrays into `a.b[0].c` = value rows. */
export function flatten(value: unknown, prefix = ''): KV[] {
  if (isScalar(value) || (Array.isArray(value) && value.every(isScalar))) return prefix ? [{ path: prefix, value: formatValue(value) }] : []
  if (Array.isArray(value)) return value.flatMap((v, i) => flatten(v, `${prefix}[${i}]`))
  return Object.entries(value as Record<string, unknown>).flatMap(([k, v]) => flatten(v, prefix ? `${prefix}.${k}` : k))
}
