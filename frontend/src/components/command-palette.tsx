import { useNavigate, useRouterState } from '@tanstack/react-router'
import { CirclePause, CirclePlay, Eye, EyeOff, FileText, Keyboard, Languages, Moon, Plus } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from '@/components/ui/command'
import { toggleLanguage } from '@/i18n'
import { PAUSE_OPTIONS, usePauseLlm, useResumeLlm } from '@/features/llm/api'
import { TradeDialog } from '@/features/portfolio/trade-dialog'
import { useSearch } from '@/features/search/api'
import { errorText } from '@/features/shared/states'
import { useUnwatch, useWatch } from '@/features/watchlist/api'
import { NAV } from '@/lib/nav'
import { toggleTheme } from '@/lib/theme'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  onShortcuts: () => void
}

export function CommandPalette({ open, onOpenChange, onShortcuts }: Props) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [tradeOpen, setTradeOpen] = useState(false)
  const pause = usePauseLlm()
  const resume = useResumeLlm()
  const watch = useWatch()
  const unwatch = useUnwatch()
  // On an instrument page the trade / watch commands act on that ticker.
  const pathname = useRouterState({ select: (st) => st.location.pathname })
  const pageTicker = /^\/i\/([^/]+)/.exec(pathname)?.[1]?.toUpperCase()

  const [query, setQuery] = useState('')
  const search = useSearch(query, open)
  const hits = query.trim() ? search.data : undefined

  const run = (fn: () => void) => () => {
    onOpenChange(false)
    fn()
  }

  const fail = (e: unknown) => toast.error(`${t('palette.actionFailed')}: ${errorText(e) ?? ''}`)
  const doPause = (minutes: number | null) => pause.mutate({ minutes }, { onSuccess: () => toast.success(t('palette.pauseOk')), onError: fail })
  const doResume = () => resume.mutate(undefined, { onSuccess: () => toast.success(t('palette.resumeOk')), onError: fail })
  const doWatch = (ticker: string) => watch.mutate({ ref: ticker }, { onSuccess: () => toast.success(t('watchlist.watched', { ticker })), onError: fail })
  const doUnwatch = (ticker: string) => unwatch.mutate({ ref: ticker }, { onSuccess: () => toast.success(t('watchlist.unwatched', { ticker })), onError: fail })

  return (
    <>
    <CommandDialog
      open={open}
      onOpenChange={(o) => {
        if (!o) setQuery('')
        onOpenChange(o)
      }}
      title={t('palette.title')} description={t('palette.description')}
    >
      <CommandInput placeholder={t('palette.placeholder')} value={query} onValueChange={setQuery} />
      <CommandList>
        {!(hits && (hits.instruments.length > 0 || hits.disclosures.length > 0)) && <CommandEmpty>{t('palette.empty')}</CommandEmpty>}
        <CommandGroup heading={t('palette.navigate')}>
          {NAV.map(({ key, to, icon: Icon, hotkey }) => (
            <CommandItem key={key} value={`${t(`nav.${key}`)} ${key}`} onSelect={run(() => void navigate({ to }))}>
              <Icon />
              {t(`nav.${key}`)}
              <CommandShortcut>g {hotkey}</CommandShortcut>
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandGroup heading={t('palette.actions')}>
          <CommandItem onSelect={run(toggleTheme)}>
            <Moon />
            {t('palette.toggleTheme')}
          </CommandItem>
          <CommandItem onSelect={run(toggleLanguage)}>
            <Languages />
            {t('palette.toggleLanguage')}
          </CommandItem>
          <CommandItem onSelect={run(() => setTradeOpen(true))} value="trade add islem ekle">
            <Plus />
            {t('portfolio.trade.palette')}
          </CommandItem>
          {pageTicker && (
            <>
              <CommandItem onSelect={run(() => doWatch(pageTicker))} value={`watch ${pageTicker} izle`}>
                <Eye />
                {t('watchlist.palette.watch', { ticker: pageTicker })}
              </CommandItem>
              <CommandItem onSelect={run(() => doUnwatch(pageTicker))} value={`unwatch ${pageTicker} izlemeyi birak`}>
                <EyeOff />
                {t('watchlist.palette.unwatch', { ticker: pageTicker })}
              </CommandItem>
            </>
          )}
          <CommandItem onSelect={run(() => doPause(null))}>
            <CirclePause />
            {t('palette.pauseLlm')}
          </CommandItem>
          {PAUSE_OPTIONS.filter((o) => o.minutes !== null).map((o) => (
            <CommandItem key={o.key} onSelect={run(() => doPause(o.minutes))} value={`pause llm ${o.key}`}>
              <CirclePause />
              {t('llm.palette.pauseFor', { duration: t(`llm.duration.${o.key}`) })}
            </CommandItem>
          ))}
          <CommandItem onSelect={run(doResume)}>
            <CirclePlay />
            {t('palette.resumeLlm')}
          </CommandItem>
          <CommandItem onSelect={run(onShortcuts)}>
            <Keyboard />
            {t('palette.shortcuts')}
            <CommandShortcut>?</CommandShortcut>
          </CommandItem>
        </CommandGroup>
        {hits && hits.instruments.length > 0 && (
          <CommandGroup heading={t('palette.tickers')} forceMount>
            {hits.instruments.map((h) => (
              <CommandItem
                key={h.instrument_id}
                forceMount
                value={`instrument-${h.ticker}`}
                onSelect={run(() => void navigate({ to: '/i/$ticker', params: { ticker: h.ticker } }))}
              >
                <span className="font-mono font-medium">{h.ticker}</span>
                {h.badge && (
                  <span className="rounded-sm border border-border px-1 text-xs text-muted-foreground">
                    {t(h.badge === 'HOLDING' ? 'search.holding' : 'search.watch')}
                  </span>
                )}
                <span className="min-w-0 flex-1 truncate text-muted-foreground">{h.name}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {hits && hits.disclosures.length > 0 && (
          <CommandGroup heading={t('search.disclosures')} forceMount>
            {hits.disclosures.map((h) =>
              h.event_id ? (
                <CommandItem
                  key={h.disclosure_id}
                  forceMount
                  value={`disclosure-${h.disclosure_id}`}
                  onSelect={run(() => void navigate({ to: '/feed/$eventId', params: { eventId: h.event_id! } }))}
                >
                  <FileText />
                  <span className="min-w-0 flex-1 truncate">{h.headline}</span>
                  {h.tickers.slice(0, 2).map((tk) => (
                    <span key={tk} className="font-mono text-xs text-muted-foreground">{tk}</span>
                  ))}
                </CommandItem>
              ) : null,
            )}
          </CommandGroup>
        )}
        {query.trim() && search.pending && !hits && (
          <div className="px-3 py-2 text-xs text-muted-foreground">{t('search.searching')}</div>
        )}
      </CommandList>
    </CommandDialog>
      <TradeDialog open={tradeOpen} onOpenChange={setTradeOpen} defaultTicker={pageTicker} />
    </>
  )
}
