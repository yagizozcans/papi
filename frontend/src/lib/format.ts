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
