import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CirclePause, CirclePlay, Keyboard, Languages, Moon, Search } from 'lucide-react'
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
    <CommandDialog open={open} onOpenChange={onOpenChange} title={t('palette.title')} description={t('palette.description')}>
      <CommandInput placeholder={t('palette.placeholder')} />
      <CommandList>
        <CommandEmpty>{t('palette.empty')}</CommandEmpty>
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
        <CommandGroup heading={t('palette.tickers')}>
          <CommandItem disabled value="tickers-soon">
            <Search />
            {t('palette.tickersSoon')}
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
