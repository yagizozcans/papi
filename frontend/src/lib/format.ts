import i18n from '@/i18n'

export type Tone = 'ok' | 'warn' | 'bad' | 'muted'

export function lagTone(seconds: number | null | undefined): Tone {
  if (seconds == null) return 'bad'
  if (seconds < 120) return 'ok'
  if (seconds < 600) return 'warn'
  return 'bad'
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s}s`
  if (s < 90 * 60) return `${Math.round(s / 60)}m`
  if (s < 48 * 3600) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}

/** Age of an ISO timestamp relative to `now` (the server clock avoids browser clock skew). */
export function ageSeconds(iso: string, now: string): number | null {
  const a = Date.parse(iso)
  const b = Date.parse(now)
  return Number.isNaN(a) || Number.isNaN(b) ? null : (b - a) / 1000
}

export const toneClass: Record<Tone, string> = {
  ok: 'text-up',
  warn: 'text-warning',
  bad: 'text-error',
  muted: 'text-muted-foreground',
}

/* ---- Locale-aware number formatting (Turkish: 1.234,56 ; English: 1,234.56) ---- */


export const TIME_ZONE = 'Europe/Istanbul'

function locale(lang?: string): 'tr-TR' | 'en-US' {
  return (lang ?? i18n.language) === 'en' ? 'en-US' : 'tr-TR'
}

const nfCache = new Map<string, Intl.NumberFormat>()
function nf(lang: string | undefined, o: Intl.NumberFormatOptions): Intl.NumberFormat {
  const loc = locale(lang)
  const key = loc + JSON.stringify(o)
  let f = nfCache.get(key)
  if (!f) {
    f = new Intl.NumberFormat(loc, o)
    nfCache.set(key, f)
  }
  return f
}

export const DASH = '–'

export interface NumOptions {
  decimals?: number
  /** Show an explicit "+" for positive values. */
  signed?: boolean
  /** Compact notation (1,2 Mn / 1.2M). */
  compact?: boolean
  lang?: string
}

/** Plain number. null/undefined/NaN render as an en dash. */
export function formatNum(value: number | null | undefined, { decimals = 2, signed, compact, lang }: NumOptions = {}): string {
  if (value == null || !Number.isFinite(value)) return DASH
  const f = nf(lang, {
    minimumFractionDigits: compact ? 0 : decimals,
    maximumFractionDigits: decimals,
    notation: compact ? 'compact' : 'standard',
    signDisplay: signed ? 'exceptZero' : 'auto',
  })
  return f.format(value)
}

const SYMBOLS: Record<string, string> = { TRY: '₺', USD: '$', EUR: '€', GBP: '£' }

/** Money: "1.234,56 ₺" in Turkish, "₺1,234.56" in English. */
export function formatMoney(
  value: number | null | undefined,
  { currency = 'TRY', decimals = 2, signed, compact, lang }: NumOptions & { currency?: string } = {},
): string {
  if (value == null || !Number.isFinite(value)) return DASH
  const sym = SYMBOLS[currency] ?? currency
  const n = formatNum(value, { decimals, signed, compact, lang })
  return locale(lang) === 'tr-TR' ? `${n}\u00A0${sym}` : n.replace(/^([+\-\u2212]?)/, `$1${sym}`)
}

/** Percent from a ratio-already-in-percent value (4.2 -> "4,20%"). */
export function formatPercent(value: number | null | undefined, { decimals = 2, signed, lang }: NumOptions = {}): string {
  if (value == null || !Number.isFinite(value)) return DASH
  const n = formatNum(value, { decimals, signed, lang })
  return locale(lang) === 'tr-TR' ? `%${n}` : `${n}%`
}

const dtCache = new Map<string, Intl.DateTimeFormat>()
/** Absolute date-time in Europe/Istanbul. */
export function formatDateTime(iso: string | number | Date, { seconds = true, lang }: { seconds?: boolean; lang?: string } = {}): string {
  const d = iso instanceof Date ? iso : new Date(iso)
  if (Number.isNaN(d.getTime())) return DASH
  const loc = locale(lang)
  const key = loc + seconds
  let f = dtCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat(loc, {
      timeZone: TIME_ZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: seconds ? '2-digit' : undefined,
      hour12: false,
    })
    dtCache.set(key, f)
  }
  return f.format(d)
}

export type RelativeUnit = 'now' | 's' | 'm' | 'h' | 'd'

/** Splits an age in seconds into a unit and amount ("3 dk önce" = { unit: 'm', n: 3 }). */
export function relativeParts(ageSec: number): { unit: RelativeUnit; n: number } {
  const s = Math.max(0, ageSec)
  if (s < 10) return { unit: 'now', n: 0 }
  if (s < 60) return { unit: 's', n: Math.floor(s) }
  if (s < 3600) return { unit: 'm', n: Math.floor(s / 60) }
  if (s < 86400) return { unit: 'h', n: Math.floor(s / 3600) }
  return { unit: 'd', n: Math.floor(s / 86400) }
}
