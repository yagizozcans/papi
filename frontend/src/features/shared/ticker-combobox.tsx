import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { useSearch } from '../search/api'

interface Props {
  value: string
  onChange: (ticker: string) => void
  placeholder?: string
  id?: string
  className?: string
  invalid?: boolean
  /** Also offer a free-form entry as-is (e.g. `endeks:XU030`, `sektor:bank`). */
  allowFreeForm?: boolean
  ariaLabel?: string
}

/** Text input with a /v1/search backed suggestion list (holdings first). Value is the ticker text. */
export function TickerCombobox({ value, onChange, placeholder, id, className, invalid, allowFreeForm, ariaLabel }: Props) {
  const { t } = useTranslation()
  const listId = useId()
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const search = useSearch(value, open)
  const hits = value.trim() ? (search.data?.instruments ?? []).slice(0, 8) : []

  const pick = (ticker: string) => {
    onChange(ticker)
    setOpen(false)
  }

  return (
    <div className={cn('relative', className)}>
      <input
        id={id}
        aria-label={ariaLabel}
        role="combobox"
        aria-expanded={open && hits.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-invalid={invalid || undefined}
        autoComplete="off"
        spellCheck={false}
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          onChange(e.target.value)
          setOpen(true)
          setActive(0)
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            e.preventDefault()
            setOpen(true)
            setActive((a) => Math.min(a + 1, Math.max(hits.length - 1, 0)))
          } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            setActive((a) => Math.max(a - 1, 0))
          } else if (e.key === 'Enter' && open && hits[active]) {
            e.preventDefault()
            pick(hits[active]!.ticker)
          } else if (e.key === 'Escape' && open) {
            e.stopPropagation()
            setOpen(false)
          }
        }}
        className={cn(
          'h-8 w-full rounded-md border border-input bg-transparent px-2 font-mono text-sm uppercase outline-none placeholder:normal-case placeholder:text-muted-foreground focus-visible:border-ring',
          invalid && 'border-error',
        )}
      />
      {open && (hits.length > 0 || (allowFreeForm && value.trim())) && (
        <ul id={listId} role="listbox" className="absolute left-0 right-0 top-full z-50 mt-1 max-h-60 overflow-auto rounded-md border border-border bg-popover p-1 shadow-md">
          {hits.map((h, i) => (
            <li
              key={h.instrument_id}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => {
                e.preventDefault()
                pick(h.ticker)
              }}
              onMouseEnter={() => setActive(i)}
              className={cn('flex cursor-default items-center gap-2 rounded-sm px-2 py-1 text-sm', i === active && 'bg-accent')}
            >
              <span className="font-mono font-medium">{h.ticker}</span>
              {h.badge && <span className="rounded-sm border border-border px-1 text-xs text-muted-foreground">{t(h.badge === 'HOLDING' ? 'search.holding' : 'search.watch')}</span>}
              <span className="min-w-0 flex-1 truncate text-muted-foreground">{h.name}</span>
            </li>
          ))}
          {allowFreeForm && hits.length === 0 && <li className="px-2 py-1 text-xs text-muted-foreground">{t('watchlist.freeFormHint')}</li>}
        </ul>
      )}
    </div>
  )
}
