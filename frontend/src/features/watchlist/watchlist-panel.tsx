import { Plus, X } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { TickerChip } from '@/components/kit/ticker-chip'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import type { Overview } from '../portfolio/api'
import { errorText } from '../shared/states'
import { TickerCombobox } from '../shared/ticker-combobox'
import { DEFAULT_WATCHLIST, useUnwatch, useWatch } from './api'

const mark = (b: 'HOLDING' | 'WATCH' | null | undefined) => (b === 'HOLDING' ? 'holding' : b === 'WATCH' ? 'watch' : null)

/** Watchlists with remove buttons and an add box (ticker, `XU030`, `endeks:XU030`, `sektor:<kod>`). */
export function WatchlistPanel({ lists }: { lists: Overview['watchlists'] }) {
  const { t } = useTranslation()
  const watch = useWatch()
  const unwatch = useUnwatch()
  const [text, setText] = useState('')
  const target = lists[0]?.name ?? DEFAULT_WATCHLIST

  const remove = (ref: string, watchlist: string) =>
    unwatch.mutate(
      { ref, watchlist },
      { onSuccess: () => toast.success(t('watchlist.unwatched', { ticker: ref })), onError: (e) => toast.error(errorText(e) ?? t('watchlist.failed')) },
    )

  const add = (e: FormEvent) => {
    e.preventDefault()
    const ref = text.trim()
    if (!ref) return
    watch.mutate(
      { ref, watchlist: target },
      {
        onSuccess: (r) => {
          setText('')
          toast.success(r.changed ? t('watchlist.watched', { ticker: ref.toUpperCase() }) : t('watchlist.already', { ticker: ref.toUpperCase() }))
        },
        onError: (err) => toast.error(errorText(err) ?? t('watchlist.failed')),
      },
    )
  }

  const removeButton = (label: string, onClick: () => void) => (
    <button type="button" aria-label={t('watchlist.remove', { name: label })} title={t('watchlist.remove', { name: label })} onClick={onClick} disabled={unwatch.isPending} className="rounded-sm p-0.5 text-muted-foreground hover:bg-accent hover:text-error">
      <X className="size-3" />
    </button>
  )

  return (
    <div>
      {lists.length === 0 ? (
        <EmptyState title={t('portfolio.watchlists.empty')} className="py-4" />
      ) : (
        <ul>
          {lists.map((w) => (
            <li key={w.id} className="flex flex-col gap-1.5 border-b border-border px-3 py-2" data-testid="watchlist">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{w.name}</span>
                <span className="num text-xs text-muted-foreground">{w.members.length}</span>
              </div>
              {(w.members.length > 0 || w.sectors.length > 0 || w.indices.length > 0) && (
                <div className="flex flex-wrap gap-1">
                  {w.members.map((m) => (
                    <span key={m.instrument_id} className="inline-flex items-center gap-0.5">
                      <TickerChip ticker={m.ticker} mark={mark(m.badge)} />
                      {removeButton(m.ticker, () => remove(m.ticker, w.name))}
                    </span>
                  ))}
                  {w.indices.map((x) => (
                    <span key={`i-${x}`} className="inline-flex items-center gap-0.5">
                      <Badge variant="outline">{x}</Badge>
                      {removeButton(x, () => remove(`endeks:${x}`, w.name))}
                    </span>
                  ))}
                  {w.sectors.map((x) => (
                    <span key={`s-${x}`} className="inline-flex items-center gap-0.5">
                      <Badge variant="outline">{x}</Badge>
                      {removeButton(x, () => remove(`sektor:${x}`, w.name))}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={add} className="flex items-center gap-2 p-3" aria-label={t('watchlist.add')}>
        <TickerCombobox value={text} onChange={setText} allowFreeForm ariaLabel={t('watchlist.placeholder')} placeholder={t('watchlist.placeholder')} className="min-w-0 flex-1" />
        <Button type="submit" size="sm" variant="outline" disabled={watch.isPending || !text.trim()}>
          <Plus />
          {t('watchlist.add')}
        </Button>
      </form>
    </div>
  )
}
