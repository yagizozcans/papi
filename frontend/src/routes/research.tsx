import { createFileRoute, Link, Outlet } from '@tanstack/react-router'
import { useTranslation } from 'react-i18next'

const TABS = [
  { to: '/research', key: 'overview', exact: true },
  { to: '/research/theses', key: 'theses', exact: false },
  { to: '/research/agents', key: 'agents', exact: false },
  { to: '/research/kb', key: 'kb', exact: false },
] as const

function ResearchLayout() {
  const { t } = useTranslation()
  return (
    <>
      <nav className="flex h-9 items-end gap-4 border-b border-border px-4 md:px-6">
        {TABS.map(({ to, key, exact }) => (
          <Link
            key={key}
            to={to}
            activeOptions={{ exact }}
            className="-mb-px border-b-2 border-transparent pb-2 text-muted-foreground hover:text-foreground"
            activeProps={{ className: 'border-brand text-foreground' }}
          >
            {t(`research.${key}`)}
          </Link>
        ))}
      </nav>
      <Outlet />
    </>
  )
}

export const Route = createFileRoute('/research')({ component: ResearchLayout })
