import { X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { type Priority, priorityClass } from './priority-badge'

export interface FilterState {
  scope?: string
  priorities?: Priority[]
  range?: string
  ticker?: string
  family?: string
}

export interface FilterOption {
  value: string
  label: string
}

const DEFAULT_RANGES = ['all', 'today', '7d', '30d', '90d']

export interface FilterBarProps {
  value: FilterState
  onChange: (next: FilterState) => void
  /** Scope / family selects only appear when options are given. */
  scopeOptions?: FilterOption[]
  familyOptions?: FilterOption[]
  /** Date range presets (value ids); labels come from i18n `kit.filter.ranges.<id>` unless `rangeOptions` is used. */
  rangeOptions?: FilterOption[]
  className?: string
}

const selectClass =
  'h-7 rounded-md border border-input bg-transparent px-2 text-sm text-foreground outline-none hover:bg-accent focus-visible:border-ring'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

/** Controlled filter chips/selects. Bind `value`/`onChange` to router search params. */
export function FilterBar({ value, onChange, scopeOptions, familyOptions, rangeOptions, className }: FilterBarProps) {
  const { t } = useTranslation()
  const set = (patch: FilterState) => onChange({ ...value, ...patch })

  // Ticker is typed freely: keep a local draft and commit shortly after the last keystroke.
  const [draft, setDraft] = useState(value.ticker ?? '')
  const [prevTicker, setPrevTicker] = useState(value.ticker)
  if (prevTicker !== value.ticker) {
    setPrevTicker(value.ticker)
    setDraft(value.ticker ?? '')
  }
  useEffect(() => {
    const next = draft.trim().toUpperCase()
    if (next === (value.ticker ?? '')) return
    const timer = window.setTimeout(() => onChange({ ...value, ticker: next || undefined }), 250)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  const ranges = rangeOptions ?? DEFAULT_RANGES.map((id) => ({ value: id, label: t(`kit.filter.ranges.${id}`) }))
  const selected = value.priorities ?? []
  const active =
    !!value.scope || selected.length > 0 || (!!value.range && value.range !== 'all') || !!value.ticker || !!value.family

  const togglePriority = (p: Priority) =>
    set({ priorities: selected.includes(p) ? selected.filter((x) => x !== p) : [...selected, p].sort() })

  return (
    <div role="search" aria-label={t('kit.filter.label')} className={cn('flex flex-wrap items-center gap-x-3 gap-y-1.5', className)}>
      {scopeOptions && (
        <Field label={t('kit.filter.scope')}>
          <select className={selectClass} value={value.scope ?? ''} onChange={(e) => set({ scope: e.target.value || undefined })}>
            <option value="">{t('kit.filter.all')}</option>
            {scopeOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
      )}

      <div role="group" aria-label={t('kit.filter.priority')} className="flex items-center gap-1">
        {(['P1', 'P2', 'P3'] as const).map((p) => {
          const on = selected.includes(p)
          return (
            <button
              key={p}
              type="button"
              aria-pressed={on}
              onClick={() => togglePriority(p)}
              className={cn(
                'h-7 min-w-9 rounded-md border px-2 font-mono text-xs font-semibold',
                on ? priorityClass[p] : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
              )}
            >
              {p}
            </button>
          )
        })}
      </div>

      <Field label={t('kit.filter.range')}>
        <select className={selectClass} value={value.range ?? 'all'} onChange={(e) => set({ range: e.target.value === 'all' ? undefined : e.target.value })}>
          {ranges.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>

      <Field label={t('kit.filter.ticker')}>
        <input
          type="search"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={t('kit.filter.tickerPlaceholder')}
          autoCapitalize="characters"
          spellCheck={false}
          className={cn(selectClass, 'num w-24 uppercase placeholder:normal-case placeholder:text-muted-foreground/60')}
        />
      </Field>

      {familyOptions && (
        <Field label={t('kit.filter.family')}>
          <select className={selectClass} value={value.family ?? ''} onChange={(e) => set({ family: e.target.value || undefined })}>
            <option value="">{t('kit.filter.all')}</option>
            {familyOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
      )}

      {active && (
        <Button variant="ghost" size="sm" onClick={() => onChange({})} className="text-muted-foreground">
          <X /> {t('kit.filter.clear')}
        </Button>
      )}
    </div>
  )
}
