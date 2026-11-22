import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CirclePause, CirclePlay, FileText, Keyboard, Languages, Moon } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { STATUS_KEY } from '@/api/status'
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
import { useSearch } from '@/features/search/api'
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
  const queryClient = useQueryClient()

  const [query, setQuery] = useState('')
  const search = useSearch(query, open)
  const hits = query.trim() ? search.data : undefined

  const run = (fn: () => void) => () => {
    onOpenChange(false)
    fn()
  }

  const setLlm = (action: 'pause' | 'resume') => async () => {
    try {
      const { response } =
        action === 'pause' ? await api.POST('/v1/llm/pause') : await api.POST('/v1/llm/resume')
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      toast.success(t(action === 'pause' ? 'palette.pauseOk' : 'palette.resumeOk'))
    } catch (e) {
      toast.error(`${t('palette.actionFailed')}: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  }

  return (
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
          <CommandItem onSelect={run(() => void setLlm('pause')())}>
            <CirclePause />
            {t('palette.pauseLlm')}
          </CommandItem>
          <CommandItem onSelect={run(() => void setLlm('resume')())}>
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
  )
}
