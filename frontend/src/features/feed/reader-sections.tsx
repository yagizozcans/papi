import { type ReactNode, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from '@tanstack/react-router'
import { ExternalLink } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Panel } from '@/components/ui/panel'
import { formatDateTime, formatMoney, formatNum, formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'
import { type EventDetail, type FeedbackKind, useFeedback } from './api'
import { flatten, formatValue, type KV } from './lib'

type Detail = EventDetail

/* ---- shared bits ---- */

export function KeyValueTable({ rows, className }: { rows: KV[]; className?: string }) {
  return (
    <table className={cn('w-full text-sm', className)}>
      <tbody>
        {rows.map((r) => (
          <tr key={r.path} className="border-b border-border last:border-b-0">
            <th scope="row" className="w-2/5 max-w-0 break-words px-3 py-1 text-left align-top font-mono text-xs font-normal text-muted-foreground">
              {r.path}
            </th>
            <td className="break-words px-3 py-1 align-top">{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function Collapsible({ title, children, defaultOpen }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details open={defaultOpen} className="group rounded-md border border-border bg-card">
      <summary className="flex h-9 cursor-default select-none items-center gap-2 px-3 text-xs font-medium uppercase tracking-wide text-muted-foreground marker:content-none hover:text-foreground [&::-webkit-details-marker]:hidden">
        <span aria-hidden="true" className="transition-transform group-open:rotate-90">
          ▸
        </span>
        {title}
      </summary>
      <div className="border-t border-border">{children}</div>
    </details>
  )
}

const num = (n: number | null | undefined, d = 2) => formatNum(n, { decimals: d })

/** `[S3]` button that scrolls to and highlights the cited span in the document. */
export function Cite({ anchor, quote, onCite }: { anchor: string; quote?: string | null; onCite: (anchor: string) => void }) {
  const { t } = useTranslation()
  const n = /s(\d+)$/.exec(anchor)?.[1] ?? '?'
  return (
    <button
      type="button"
      onClick={() => onCite(anchor)}
      title={quote ? `${t('feed.analysis.showSource')}: ${quote}` : t('feed.analysis.showSource')}
      className="mx-0.5 rounded-sm border border-brand/40 bg-brand/10 px-1 font-mono text-xs text-brand hover:bg-brand hover:text-primary-foreground"
    >
      [S{n}]
    </button>
  )
}

/** Renders text with `[S3]` markers as citation buttons (when a matching anchor is known). */
function TextWithRefs({ text, anchors, onCite }: { text: string; anchors: Map<string, { anchor: string; quote: string | null }>; onCite: (a: string) => void }) {
  const parts = text.split(/(\[S\d+\])/g)
  return (
    <>
      {parts.map((part, i) => {
        const m = /^\[S(\d+)\]$/.exec(part)
        const hit = m ? anchors.get(m[1]!) : undefined
        return hit ? <Cite key={i} anchor={hit.anchor} quote={hit.quote} onCite={onCite} /> : <span key={i}>{part}</span>
      })}
    </>
  )
}

function anchorMap(items: { anchor: string | null; quote?: string | null }[]) {
  const map = new Map<string, { anchor: string; quote: string | null }>()
  for (const c of items) {
    const n = c.anchor ? /s(\d+)$/.exec(c.anchor)?.[1] : undefined
    if (c.anchor && n && !map.has(n)) map.set(n, { anchor: c.anchor, quote: c.quote ?? null })
  }
  return map
}

/* ---- sections ---- */

export function ForMe({ detail }: { detail: Detail }) {
  const { t } = useTranslation()
  const fm = detail.for_me
  return (
    <Panel title={t('feed.sections.forMe')} actions={fm.holding ? <Badge variant="brand">{t('feed.forMe.holding')}</Badge> : undefined} aria-label={t('feed.sections.forMe')}>
      <div className="flex flex-col gap-3 p-3">
        {fm.positions.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="pr-3 font-normal">{t('feed.forMe.ticker')}</th>
                  <th className="px-3 text-right font-normal">{t('feed.forMe.quantity')}</th>
                  <th className="px-3 text-right font-normal">{t('feed.forMe.avgCost')}</th>
                  <th className="px-3 text-right font-normal">{t('feed.forMe.lastClose')}</th>
                  <th className="px-3 text-right font-normal">{t('feed.forMe.value')}</th>
                  <th className="pl-3 text-right font-normal">{t('feed.forMe.weight')}</th>
                </tr>
              </thead>
              <tbody>
                {fm.positions.map((p) => (
                  <tr key={p.ticker} className="border-t border-border">
                    <td className="py-1 pr-3 font-mono">{p.ticker}</td>
                    <td className="num px-3 text-right">{num(p.quantity, 0)}</td>
                    <td className="num px-3 text-right">{formatMoney(p.avg_cost)}</td>
                    <td className="num px-3 text-right">{formatMoney(p.last_close)}</td>
                    <td className="num px-3 text-right">{formatMoney(p.market_value)}</td>
                    <td className="num pl-3 text-right">{p.weight != null ? formatPercent(p.weight * 100, { decimals: 1 }) : '–'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {fm.impacts.map((im) => (
          <div key={im.forecast_id} className="rounded-md border border-border">
            <div className="flex items-center gap-2 border-b border-border px-3 py-1 text-sm">
              <Badge variant="outline">{t('feed.forecast.model')}</Badge>
              <b className="font-mono">
                {im.ticker} {im.horizon}
              </b>
              <span className="text-xs text-muted-foreground">{im.model}</span>
            </div>
            <KeyValueTable rows={flatten(im.position_impact)} />
          </div>
        ))}
        {fm.reason && <p className="text-sm text-muted-foreground">{fm.reason}</p>}
        {fm.positions.length === 0 && fm.impacts.length === 0 && !fm.reason && <p className="text-sm text-muted-foreground">{t('feed.forMe.none')}</p>}
      </div>
    </Panel>
  )
}

export function Summary({ detail, lang, onCite }: { detail: Detail; lang: string; onCite: (a: string) => void }) {
  const { t } = useTranslation()
  const { tr, en } = detail.summaries
  const anchors = anchorMap(detail.analyses.flatMap((a) => a.claims.flatMap((c) => c.citations)))
  const primary = lang === 'en' && en ? en : (tr ?? en)
  const primaryLang = primary === en && primary !== tr ? 'en' : 'tr'
  const other = primary === tr ? en : tr
  return (
    <Panel title={t('feed.sections.summary')} aria-label={t('feed.sections.summary')}>
      <div className="flex flex-col gap-2 p-3 text-sm">
        {primary ? (
          <div lang={primaryLang}>
            <Badge variant="outline" className="mb-1">
              {t('feed.summary.interpretation')}
            </Badge>
            {primary.headline && <p className="font-medium">{primary.headline}</p>}
            {primary.body && (
              <p className="whitespace-pre-line">
                <TextWithRefs text={primary.body} anchors={anchors} onCite={onCite} />
              </p>
            )}
          </div>
        ) : (
          <p className="text-muted-foreground">
            {detail.item.stage === 'rules' ? `${t('feed.summary.noAnalysis')} ` : ''}
            {t('feed.summary.none')}
          </p>
        )}
        {other && (
          <details lang={other === en ? 'en' : 'tr'}>
            <summary className="cursor-default text-xs text-muted-foreground hover:text-foreground">{other === en ? 'English' : 'Türkçe'}</summary>
            {other.headline && <p className="mt-1 font-medium">{other.headline}</p>}
            {other.body && <p className="whitespace-pre-line">{other.body}</p>}
          </details>
        )}
      </div>
    </Panel>
  )
}

export function KeyFacts({ detail, onCite }: { detail: Detail; onCite: (a: string) => void }) {
  const { t } = useTranslation()
  const facts = detail.key_facts
  return (
    <Panel title={t('feed.sections.facts')} aria-label={t('feed.sections.facts')}>
      {facts.length === 0 ? (
        <p className="p-3 text-sm text-muted-foreground">{t('feed.facts.none')}</p>
      ) : (
        <table className="w-full text-sm">
          <tbody>
            {facts.map((f) => (
              <tr key={f.path} className="border-b border-border last:border-b-0">
                <th scope="row" className="w-2/5 max-w-0 break-words px-3 py-1 text-left align-top font-mono text-xs font-normal text-muted-foreground">
                  {f.path}
                </th>
                <td className="break-words px-3 py-1 align-top">
                  {formatValue(f.value)}
                  {f.anchors.map((a) => (
                    <Cite key={a} anchor={a} onCite={onCite} />
                  ))}
                  {f.anchors.length === 0 && <small className="ml-1 text-muted-foreground">({t('feed.facts.unmapped')})</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}

const KIND_KEY: Record<string, string> = { fact: 'fact', computation: 'calc', inference: 'interp' }

export function Analyses({ detail, onCite }: { detail: Detail; onCite: (a: string) => void }) {
  const { t, i18n } = useTranslation()
  if (detail.analyses.length === 0) return null
  return (
    <Panel title={t('feed.sections.analysis')} aria-label={t('feed.sections.analysis')}>
      <div className="flex flex-col divide-y divide-border">
        {detail.analyses.map((a) => (
          <div key={a.id} className="flex flex-col gap-2 p-3">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <b className="text-foreground">{a.agent}</b>
              <span>{formatDateTime(a.created_at, { lang: i18n.language })}</span>
              {a.model_ids.length > 0 && <span className="font-mono">{a.model_ids.join(', ')}</span>}
              {a.confidence != null && <span>{t('feed.analysis.confidence', { value: num(a.confidence) })}</span>}
              <Badge variant={a.review_state === 'approved' ? 'up' : 'outline'}>{a.review_state}</Badge>
            </div>
            {a.claims.length > 0 && (
              <ul className="flex flex-col gap-1.5">
                {a.claims.map((c) => {
                  const text = (i18n.language === 'en' && c.text_en) || c.text_tr || c.text_en || ''
                  const map = anchorMap(c.citations)
                  const inText = new Set([...text.matchAll(/\[S(\d+)\]/g)].map((m) => m[1]))
                  const extra = [...map.entries()].filter(([n]) => !inText.has(n))
                  const unverified = c.check_status === 'unsupported' || c.verify_status === 'fail'
                  return (
                    <li key={c.id} className="text-sm">
                      <Badge variant="outline" className="mr-1.5 align-middle">
                        {t(`feed.analysis.kind.${KIND_KEY[c.kind] ?? 'interp'}`, c.kind)}
                      </Badge>
                      <TextWithRefs text={text} anchors={map} onCite={onCite} />
                      {extra.map(([n, v]) => (
                        <Cite key={n} anchor={v.anchor} quote={v.quote} onCite={onCite} />
                      ))}
                      {unverified && (
                        <Badge variant="down" className="ml-1.5 align-middle">
                          {t('feed.analysis.unverified')}
                        </Badge>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
            {a.output != null && flatten(a.output).length > 0 && (
              <Collapsible title={t('feed.analysis.output')}>
                <KeyValueTable rows={flatten(a.output)} />
              </Collapsible>
            )}
          </div>
        ))}
      </div>
    </Panel>
  )
}

export function Forecasts({ detail }: { detail: Detail }) {
  const { t } = useTranslation()
  if (detail.forecasts.length === 0) return null
  return (
    <Panel title={t('feed.sections.forecasts')} aria-label={t('feed.sections.forecasts')}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
              {(['ticker', 'horizon', 'target', 'distribution', 'model', 'realised'] as const).map((k) => (
                <th key={k} className="px-3 py-1 font-normal">
                  {t(`feed.forecast.${k}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {detail.forecasts.map((f) => (
              <tr key={f.id} className="border-t border-border align-top">
                <td className="px-3 py-1 font-mono">{f.ticker}</td>
                <td className="px-3 py-1">{f.horizon}</td>
                <td className="px-3 py-1">{f.target}</td>
                <td className="px-3 py-1">
                  {f.distribution
                    ? flatten(f.distribution)
                        .map((kv) => `${kv.path}=${kv.value}`)
                        .join(' · ')
                    : '–'}
                </td>
                <td className="px-3 py-1 text-muted-foreground">{f.model}</td>
                <td className="num px-3 py-1">{f.outcome ? formatValue(f.outcome.realised_value) : '–'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

export function PayloadAndRules({ detail }: { detail: Detail }) {
  const { t } = useTranslation()
  const payload = detail.event.payload ? flatten(detail.event.payload) : []
  const rules = detail.event.rules ? flatten(detail.event.rules) : []
  return (
    <>
      <Collapsible
        title={
          <>
            {t('feed.sections.payload')}
            {detail.event.payload_schema && <span className="font-mono normal-case">· {detail.event.payload_schema}</span>}
          </>
        }
      >
        {payload.length ? <KeyValueTable rows={payload} /> : <p className="p-3 text-sm text-muted-foreground">{t('feed.facts.none')}</p>}
      </Collapsible>
      {rules.length > 0 && (
        <Collapsible title={t('feed.sections.triage')}>
          <KeyValueTable rows={rules} />
        </Collapsible>
      )}
    </>
  )
}

export function History({ detail }: { detail: Detail }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const { revision_chain: chain, superseded, disclosures } = detail
  if (chain.length === 0 && superseded.length === 0 && disclosures.length === 0) return null
  return (
    <Panel title={t('feed.sections.history')} aria-label={t('feed.sections.history')}>
      <div className="flex flex-col gap-3 p-3 text-sm">
        {chain.length > 0 && (
          <div>
            <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('feed.history.revisions')}</h3>
            <ol className="flex flex-col gap-1">
              {chain.map((r) => (
                <li key={r.id} className={cn('flex flex-wrap items-center gap-x-2', r.current && 'font-medium')}>
                  <Badge variant={r.current ? 'brand' : 'outline'}>{r.revision_label}</Badge>
                  <span className="num text-muted-foreground">{formatDateTime(r.published_at ?? '', { lang })}</span>
                  {r.event_id && !r.current && (
                    <Link to="/feed/$eventId" params={{ eventId: r.event_id }} search={(prev) => prev} className="text-brand hover:underline">
                      {t('feed.history.event')}
                    </Link>
                  )}
                  {r.kap_url && (
                    <a href={r.kap_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
                      KAP {r.source_ref} <ExternalLink className="size-3" />
                    </a>
                  )}
                </li>
              ))}
            </ol>
          </div>
        )}
        {superseded.length > 0 && (
          <div>
            <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('feed.history.superseded')}</h3>
            <ul className="flex flex-wrap gap-2">
              {superseded.map((s) => (
                <li key={s.id}>
                  <Link to="/feed/$eventId" params={{ eventId: s.id }} search={(prev) => prev} className="font-mono text-brand hover:underline">
                    {s.code}
                  </Link>{' '}
                  <span className="text-xs text-muted-foreground">{formatDateTime(s.created_at, { lang, seconds: false })}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {disclosures.length > 0 && (
          <div>
            <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('feed.history.disclosures')}</h3>
            <ul className="flex flex-col gap-1">
              {disclosures.map((d) => (
                <li key={d.id} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="num text-muted-foreground">{formatDateTime(d.published_at ?? '', { lang, seconds: false })}</span>
                  <span>{d.title ?? d.subject ?? d.source_ref}</span>
                  {d.is_late && <Badge variant="down">{t('feed.markers.late')}</Badge>}
                  {d.kap_url && (
                    <a href={d.kap_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
                      KAP {d.source_ref} <ExternalLink className="size-3" />
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  )
}

const FEEDBACK_KINDS: FeedbackKind[] = ['helpful', 'wrong_fact', 'wrong_type', 'not_material']

export function Feedback({ detail }: { detail: Detail }) {
  const { t } = useTranslation()
  const [comment, setComment] = useState('')
  const mutation = useFeedback(detail.event.id)
  return (
    <Panel title={t('feed.sections.feedback')} aria-label={t('feed.sections.feedback')}>
      <div className="flex flex-wrap items-center gap-2 p-3">
        <input
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder={t('feed.feedback.note')}
          aria-label={t('feed.feedback.note')}
          className="h-7 min-w-40 flex-1 rounded-md border border-input bg-transparent px-2 text-sm outline-none focus-visible:border-ring"
        />
        {FEEDBACK_KINDS.map((kind) => (
          <Button
            key={kind}
            variant="outline"
            size="sm"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate({ kind, comment: comment.trim() || undefined }, { onSuccess: () => setComment('') })}
          >
            {t(`feed.feedback.kinds.${kind}`)}
            {(detail.feedback_counts[kind] ?? 0) > 0 && <span className="num text-muted-foreground">({detail.feedback_counts[kind]})</span>}
          </Button>
        ))}
      </div>
    </Panel>
  )
}
