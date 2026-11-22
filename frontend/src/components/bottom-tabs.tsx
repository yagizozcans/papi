import { Link } from '@tanstack/react-router'
import { Ellipsis } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { MOBILE_TABS, NAV } from '@/lib/nav'

const tabClass = 'flex flex-1 flex-col items-center justify-center gap-0.5 text-xs text-muted-foreground'

export function BottomTabs() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const rest = NAV.filter((n) => !MOBILE_TABS.includes(n.key))

  return (
    <nav
      aria-label={t('nav.main')}
      className="flex h-14 shrink-0 border-t border-border bg-sidebar pb-[env(safe-area-inset-bottom)] md:hidden"
    >
      {NAV.filter((n) => MOBILE_TABS.includes(n.key)).map(({ key, to, icon: Icon }) => (
        <Link
          key={key}
          to={to}
          activeOptions={{ exact: to === '/' }}
          className={tabClass}
          activeProps={{ className: `${tabClass} text-brand` }}
        >
          <Icon className="size-5" />
          {t(`nav.${key}`)}
        </Link>
      ))}
      <Sheet open={open} onOpenChange={setOpen}>
        <button type="button" className={tabClass} onClick={() => setOpen(true)}>
          <Ellipsis className="size-5" />
          {t('nav.more')}
        </button>
        <SheetContent>
          <SheetTitle>{t('nav.more')}</SheetTitle>
          <SheetDescription className="sr-only">{t('nav.main')}</SheetDescription>
          {rest.map(({ key, to, icon: Icon, soon }) => (
            <SheetClose asChild key={key}>
              <Link to={to} className="flex h-10 items-center gap-3 rounded-md px-2 hover:bg-accent">
                <Icon className="size-4 text-muted-foreground" />
                <span className="flex-1">{t(`nav.${key}`)}</span>
                {soon && <span className="font-mono text-xs text-muted-foreground">{t('nav.soon')}</span>}
              </Link>
            </SheetClose>
          ))}
        </SheetContent>
      </Sheet>
    </nav>
  )
}
