import { CircleDashed } from 'lucide-react'
import { useTranslation } from 'react-i18next'

const GHOST_ROWS = 8

/** Phase P0 page body: describes what the page will hold, laid out like the finished product. */
export function PagePlaceholder({ page }: { page: string }) {
  const { t } = useTranslation()
  const items = t(`pages.${page}.items`, { returnObjects: true }) as string[]

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 md:p-6">
      <div className="flex items-start justify-between gap-4">
        <p className="max-w-prose text-muted-foreground">{t(`pages.${page}.desc`)}</p>
        <span className="shrink-0 rounded-sm border border-border px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
          {t('placeholder.badge')}
        </span>
      </div>

      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_280px]">
        <section className="overflow-hidden rounded-md border border-border bg-card" aria-hidden="true">
          <div className="flex h-8 items-center gap-6 border-b border-border px-3 text-xs uppercase tracking-wide text-muted-foreground">
            <span className="w-16">--</span>
            <span className="w-24">--</span>
            <span className="flex-1">--</span>
            <span className="num w-12 text-right">--</span>
          </div>
          {Array.from({ length: GHOST_ROWS }, (_, i) => (
            <div key={i} className="flex h-row items-center gap-6 border-b border-border px-3 last:border-b-0">
              <span className="h-2 w-16 rounded-sm bg-muted" />
              <span className="h-2 w-24 rounded-sm bg-muted" />
              <span className="h-2 flex-1 rounded-sm bg-muted" style={{ maxWidth: `${85 - ((i * 13) % 40)}%` }} />
              <span className="h-2 w-12 rounded-sm bg-muted" />
            </div>
          ))}
        </section>

        <section className="flex flex-col gap-3 rounded-md border border-border bg-card p-4">
          <div className="flex items-center gap-2 text-sm font-medium">
            <CircleDashed className="size-4 text-brand" />
            {t('placeholder.comingTitle')}
          </div>
          <p className="text-sm text-muted-foreground">{t('placeholder.comingBody')}</p>
          <h3 className="mt-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('placeholder.willShow')}</h3>
          <ul className="flex flex-col gap-2 text-sm">
            {items.map((item) => (
              <li key={item} className="flex gap-2">
                <span className="mt-2 size-1 shrink-0 rounded-full bg-muted-foreground" />
                <span>{item}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}
