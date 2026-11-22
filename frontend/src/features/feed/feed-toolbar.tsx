import { Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { FilterBar, type FilterState } from '@/components/kit/filter-bar'
import { cn } from '@/lib/utils'
import { type FeedSearch, parsePriorities, RANGES, SCOPES, SORTS, BANDS } from './search'

const selectClass =
  'h-7 rounded-md border border-input bg-transparent px-2 text-sm text-foreground outline-none hover:bg-accent focus-visible:border-ring'

export interface FeedToolbarProps {
  search: FeedSearch
  onChange: (next: FeedSearch) => void
  families: { code: string; label: string }[]
}

/** Scope, filters (kit FilterBar), full-text search, materiality band, sort and routine toggle. */
export function FeedToolbar({ search, onChange, families }: FeedToolbarProps) {
  const { t } = useTranslation()
  const scope = search.scope ?? 'mine'

  // Full-text query: local draft, committed shortly after the last keystroke.
  const [draft, setDraft] = useState(search.q ?? '')
  const [prevQ, setPrevQ] = useState(search.q)
  if (prevQ !== search.q) {
    setPrevQ(search.q)
    setDraft(search.q ?? '')
  }
  useEffect(() => {
    const next = draft.trim()
    if (next === (search.q ?? '')) return
    const timer = window.setTimeout(() => onChange({ ...search, q: next || undefined }), 300)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  const filterValue: FilterState = {
    priorities: parsePriorities(search.priority),
    range: search.range ?? 'all',
    ticker: search.ticker,
    family: search.family,
  }
  const onFilter = (next: FilterState) => {
    const range = next.range && next.range !== 'all' ? (next.range as FeedSearch['range']) : undefined
    onChange({
      ...search,
      priority: next.priorities?.length ? next.priorities.join(',') : undefined,
      range: RANGES.find((r) => r === range),
      ticker: next.ticker || undefined,
      family: next.family || undefined,
      // "Clear" in the FilterBar also resets the extra filters of this page.
      ...(!next.priorities?.length && !next.ticker && !next.family && !range ? { band: undefined, q: undefined, routine: undefined, code: undefined } : {}),
    })
  }

  return (
    <div className="flex flex-col gap-2 border-b border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <div role="radiogroup" aria-label={t('feed.scope.label')} className="flex items-center rounded-md border border-border p-0.5">
          {SCOPES.map((s) => (
            <button
              key={s}
              type="button"
              role="radio"
              aria-checked={scope === s}
              onClick={() => onChange({ ...search, scope: s === 'mine' ? undefined : s })}
              className={cn('h-6 rounded-sm px-2 text-sm', scope === s ? 'bg-accent font-medium text-foreground' : 'text-muted-foreground hover:text-foreground')}
            >
              {t(`feed.scope.${s}`)}
            </button>
          ))}
        </div>
        <label className="relative flex items-center">
          <Search aria-hidden="true" className="pointer-events-none absolute left-2 size-3.5 text-muted-foreground" />
          <input
            type="search"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={t('feed.search.placeholder')}
            aria-label={t('feed.search.label')}
            className={cn(selectClass, 'w-56 pl-7')}
          />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <FilterBar
          value={filterValue}
          onChange={onFilter}
          familyOptions={families.map((f) => ({ value: f.code, label: f.label }))}
          rangeOptions={[
            { value: 'all', label: t('kit.filter.ranges.all') },
            ...RANGES.map((r) => ({ value: r, label: t(`feed.ranges.${r}`) })),
          ]}
        />
        <label className="flex items-center gap-1.5">
          <span className="text-xs uppercase tracking-wide text-muted-foreground">{t('feed.band.label')}</span>
          <select className={selectClass} value={search.band ?? ''} onChange={(e) => onChange({ ...search, band: BANDS.find((b) => b === e.target.value) })}>
            <option value="">{t('kit.filter.all')}</option>
            {BANDS.map((b) => (
              <option key={b} value={b}>
                {t(`feed.band.${b}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          <span className="text-xs uppercase tracking-wide text-muted-foreground">{t('feed.sort.label')}</span>
          <select
            className={selectClass}
            value={search.sort ?? 'time_desc'}
            onChange={(e) => {
              const sort = SORTS.find((s) => s === e.target.value)
              onChange({ ...search, sort: sort === 'time_desc' ? undefined : sort })
            }}
          >
            {SORTS.map((s) => (
              <option key={s} value={s}>
                {t(`feed.sort.${s}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <input type="checkbox" checked={!!search.routine} onChange={(e) => onChange({ ...search, routine: e.target.checked ? true : undefined })} />
          {t('feed.hideRoutine')}
        </label>
      </div>
    </div>
  )
}
