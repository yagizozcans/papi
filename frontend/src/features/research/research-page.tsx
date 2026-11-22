import { Link } from '@tanstack/react-router'
import { Bot, BookMarked, FileText, type LucideIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { RelativeTime } from '@/components/kit/relative-time'
import { Badge } from '@/components/ui/badge'
import { useAgentRuns } from '../agents/api'
import { VerdictBadge } from '../theses/theses-page'
import { useTheses } from '../theses/api'
import { useDocumentTitle } from '../shared/use-document-title'

function Card({ icon: Icon, title, desc, children, open, to, soon }: { icon: LucideIcon; title: string; desc: string; children?: React.ReactNode; open: string; to: '/research/theses' | '/research/agents' | '/research/kb'; soon?: string }) {
  return (
    <Link to={to} className="group flex flex-col gap-2 rounded-md border border-border bg-card p-4 transition-colors hover:bg-accent/40" data-testid={`research-card-${to.split('/').pop()}`}>
      <div className="flex items-center gap-2">
        <Icon className="size-4 text-brand" />
        <h2 className="font-semibold">{title}</h2>
        {soon && <Badge variant="outline">{soon}</Badge>}
      </div>
      <p className="text-sm text-muted-foreground">{desc}</p>
      <div className="mt-auto flex flex-col gap-1 pt-1 text-sm">{children}</div>
      <span className="text-sm text-brand group-hover:underline">{open} →</span>
    </Link>
  )
}

/** /research: overview cards linking to Theses, Agents and the (P3) knowledge base. */
export function ResearchPage() {
  const { t } = useTranslation()
  useDocumentTitle(t('nav.research'))
  const theses = useTheses()
  const runs = useAgentRuns({}, 0)
  const items = theses.data?.items ?? []
  const lastReview = items.map((i) => i.last_review).filter((r) => r != null).sort((a, b) => (b.started_at ?? '').localeCompare(a.started_at ?? ''))[0]
  const activeRuns = runs.data?.items.filter((r) => r.active).length ?? 0

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 md:p-6" data-testid="research-page">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{t('research.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('research.intro')}</p>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <Card icon={FileText} to="/research/theses" title={t('research.cards.theses.title')} desc={t('research.cards.theses.desc')} open={t('research.cards.theses.open')}>
          <span className="num font-medium">{items.length > 0 ? t('research.cards.theses.count', { count: items.length }) : t('research.cards.theses.none')}</span>
          {lastReview && (
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              {t('research.cards.theses.last')}: <VerdictBadge verdict={lastReview.verdict} />
              {lastReview.started_at && <RelativeTime iso={lastReview.started_at} />}
            </span>
          )}
        </Card>
        <Card icon={Bot} to="/research/agents" title={t('research.cards.agents.title')} desc={t('research.cards.agents.desc')} open={t('research.cards.agents.open')}>
          <span className="num font-medium">{runs.data && runs.data.total > 0 ? t('research.cards.agents.count', { count: runs.data.total }) : t('research.cards.agents.none')}</span>
          {activeRuns > 0 && <span className="text-xs text-brand">{t('research.cards.agents.active', { count: activeRuns })}</span>}
        </Card>
        <Card icon={BookMarked} to="/research/kb" title={t('research.cards.kb.title')} desc={t('research.cards.kb.desc')} open={t('research.cards.kb.open')} soon={t('research.cards.kb.soon')} />
      </div>
    </div>
  )
}
