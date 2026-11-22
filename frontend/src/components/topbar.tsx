import { useMatches } from '@tanstack/react-router'
import { Languages, Moon, Search, Sun } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Logo } from '@/components/logo'
import { Button } from '@/components/ui/button'
import { toggleLanguage } from '@/i18n'
import { toggleTheme, useTheme } from '@/lib/theme'

export function Topbar({ onSearch }: { onSearch: () => void }) {
  const { t, i18n } = useTranslation()
  const theme = useTheme()
  const last = useMatches().at(-1)
  const ticker = (last?.params as { ticker?: string } | undefined)?.ticker
  const titleKey = last?.staticData?.titleKey
  const title = ticker ? ticker.toUpperCase() : titleKey ? t(titleKey) : ''

  return (
    <header className="flex h-topbar shrink-0 items-center gap-2 border-b border-border px-3 md:px-4">
      <Logo className="size-5 md:hidden" />
      <h1 data-testid="page-title" className="min-w-0 flex-1 truncate text-lg font-semibold">
        {title}
      </h1>
      <Button
        variant="outline"
        size="sm"
        onClick={onSearch}
        aria-label={t('topbar.search')}
        className="w-9 px-0 text-muted-foreground md:w-64 md:justify-start md:px-2"
      >
        <Search className="size-3.5" />
        <span className="hidden flex-1 text-left md:inline">{t('topbar.search')}</span>
        <kbd className="hidden font-mono text-xs md:inline">⌘K</kbd>
      </Button>
      <Button variant="ghost" size="sm" onClick={toggleLanguage} aria-label={t('topbar.language')} className="font-mono">
        <Languages className="size-3.5" />
        {i18n.language.toUpperCase()}
      </Button>
      <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label={t('topbar.theme')}>
        {theme === 'dark' ? <Sun className="size-4" /> : <Moon className="size-4" />}
      </Button>
    </header>
  )
}
