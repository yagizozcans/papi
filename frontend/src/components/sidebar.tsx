import { Link } from '@tanstack/react-router'
import { useTranslation } from 'react-i18next'
import { Logo } from '@/components/logo'
import { NAV } from '@/lib/nav'

export function Sidebar() {
  const { t } = useTranslation()
  return (
    <aside className="hidden w-sidebar shrink-0 flex-col border-r border-border bg-sidebar md:flex">
      <div className="flex h-topbar items-center gap-2 border-b border-border px-4">
        <Logo />
        <span className="font-mono text-base font-semibold tracking-tight">papi</span>
      </div>
      <nav aria-label={t('nav.main')} className="flex flex-col gap-0.5 p-2">
        {NAV.map(({ key, to, icon: Icon, hotkey, soon }) => (
          <Link
            key={key}
            to={to}
            activeOptions={{ exact: to === '/' }}
            className="group flex h-8 items-center gap-2 rounded-md px-2 text-muted-foreground hover:bg-accent hover:text-foreground"
            activeProps={{ className: 'bg-accent text-foreground' }}
          >
            <Icon className="size-4" />
            <span className="flex-1">{t(`nav.${key}`)}</span>
            {soon && (
              <span className="rounded-sm border border-border px-1 font-mono text-xs text-muted-foreground">
                {t('nav.soon')}
              </span>
            )}
            <kbd className="hidden text-xs text-muted-foreground/60 group-hover:inline">g {hotkey}</kbd>
          </Link>
        ))}
      </nav>
    </aside>
  )
}
