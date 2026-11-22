import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { Panel } from '@/components/ui/panel'
import { formatNum } from '@/lib/format'
import { cn } from '@/lib/utils'
import { RawJson } from '../agents/run-output'

interface Evidence {
  source?: string
  ref: string
  as_of?: string | null
  text_tr: string
}
interface Review {
  headline_tr: string
  summary_tr: string
  verdict: string
  confidence: number
  as_of: string
  edge: { category: string; stated_in_thesis?: boolean; accessible_to_individual?: string; rationale_tr?: string }
  scenarios: { name: string; probability: number; description_tr: string; drivers_tr?: string[]; signpost_tr?: string }[]
  triggers: {
    event_tr: string
    window?: string
    user_probability?: number | null
    probability: number
    status: string
    effect: string
    basis_tr?: string
    source?: string
    evidence?: Evidence[]
  }[]
  claims: { thesis_markers?: number[]; claim_tr: string; verdict: string; note_tr?: string; evidence?: Evidence[] }[]
  consensus: {
    implied_by_price_tr: string
    evidence?: Evidence[]
    thesis_section_is_consensus?: boolean
    variant_differs?: string
    note_tr?: string | null
  }
  counterparty: { who_tr: string; plausible: string; rationale_tr?: string; evidence?: Evidence[] }
  kill_signals: {
    signal_tr: string
    metric?: string
    threshold?: string
    current_value?: string | null
    current_as_of?: string | null
    evidence_ref?: string | null
    status: string
    source?: string
  }[]
  pre_mortem?: { reason_tr: string; likelihood: string }[]
  structure: { section: string; status: string; note_tr?: string }[]
  data_gaps?: string[]
}

const NEG = new Set(['contradicted', 'refuted', 'weak', 'missing', 'mislabeled', 'triggered', 'hurts'])
const POS = new Set(['supported', 'ok', 'clear', 'supports'])
const tone = (v: string) => (POS.has(v) ? 'text-up' : NEG.has(v) ? 'text-down' : '')
const badge = (v: string) => (POS.has(v) ? 'up' : NEG.has(v) ? 'down' : 'outline')
const pct = (p: number) => `%${Math.round(p * 100)}`

function Evi({ items }: { items?: Evidence[] }) {
  if (!items?.length) return null
  return (
    <>
      {items.map((e, i) => (
        <div key={i} className="mt-0.5 text-xs text-muted-foreground">
          {e.text_tr} —{' '}
          {e.ref.startsWith('http') ? (
            <a href={e.ref} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">
              {e.ref.length > 70 ? `${e.ref.slice(0, 70)}…` : e.ref}
            </a>
          ) : (
            <span className="font-mono">{e.ref}</span>
          )}
          {e.as_of && ` · ${e.as_of}`}
        </div>
      ))}
    </>
  )
}

function H({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-1 mt-3 text-xs font-medium uppercase tracking-wide text-muted-foreground first:mt-0">{children}</h3>
}

const th = 'py-1 pr-3 text-left text-xs font-medium text-muted-foreground'
const td = 'border-t border-border py-1.5 pr-3 align-top'

/** strategy-analyst output (agents/strategy-analyst/output.schema.json): every part the legacy page showed. */
export function ReviewView({ output }: { output: Record<string, unknown> }) {
  const { t, i18n } = useTranslation()
  const o = output as unknown as Review
  const lang = i18n.language
  const tr = (key: string, v: string) => t(`theses.review.${key}.${v}`, { defaultValue: v })
  const yn = (v: string | boolean | undefined) => (v === true ? t('theses.review.yes') : v === false ? t('theses.review.no') : t(`theses.review.${v}`, { defaultValue: String(v) }))

  return (
    <div className="flex flex-col gap-3" data-testid="review-view">
      <Panel title={t('theses.review.title')}>
        <div className="flex flex-col gap-2 p-3">
          <p className="text-base font-semibold leading-snug">{o.headline_tr}</p>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant={badge(o.verdict)} data-testid="review-verdict">
              {t('theses.review.verdictLabel')}: {t(`theses.verdict.${o.verdict}`, { defaultValue: o.verdict })}
            </Badge>
            <Badge variant="outline">{t('theses.review.confidence')} {formatNum(o.confidence, { decimals: 2, lang })}</Badge>
            <Badge variant="outline">{t('theses.review.asOf')} {o.as_of}</Badge>
            <Badge variant="outline">{t('theses.review.edge')}: {tr('edgeCategory', o.edge?.category)}</Badge>
          </div>
          <p className="text-sm">{o.summary_tr}</p>
        </div>
      </Panel>

      <Panel title={t('theses.review.scenarios')}>
        <div className="overflow-x-auto p-3">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={th}>{t('theses.review.scenarios')}</th>
                <th className={cn(th, 'text-right')}>{t('theses.review.probability')}</th>
                <th className={th}>{t('theses.review.whatHappens')}</th>
                <th className={th}>{t('theses.review.signpost')}</th>
              </tr>
            </thead>
            <tbody>
              {o.scenarios?.map((s) => (
                <tr key={s.name}>
                  <td className={cn(td, 'font-semibold')}>{tr('scenario', s.name)}</td>
                  <td className={cn(td, 'num text-right')}>{pct(s.probability)}</td>
                  <td className={td}>
                    {s.description_tr}
                    {s.drivers_tr && s.drivers_tr.length > 0 && (
                      <ul className="mt-1 list-disc pl-4 text-xs text-muted-foreground">
                        {s.drivers_tr.map((d, i) => (
                          <li key={i}>{d}</li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td className={td}>{s.signpost_tr}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title={t('theses.review.triggers')}>
        <div className="overflow-x-auto p-3">
          {o.triggers?.length ? (
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className={th}>{t('theses.review.event')}</th>
                  <th className={th}>{t('theses.review.window')}</th>
                  <th className={cn(th, 'text-right')}>{t('theses.review.yours')}</th>
                  <th className={cn(th, 'text-right')}>{t('theses.review.analyst')}</th>
                  <th className={th}>{t('theses.review.state')}</th>
                  <th className={th}>{t('theses.review.basis')}</th>
                </tr>
              </thead>
              <tbody>
                {o.triggers.map((x, i) => (
                  <tr key={i}>
                    <td className={td}>
                      {x.event_tr}
                      {x.source === 'agent' && <span className="ml-1 text-xs text-muted-foreground">({t('theses.review.addedByAnalyst')})</span>}
                    </td>
                    <td className={td}>{x.window}</td>
                    <td className={cn(td, 'num text-right')}>{x.user_probability != null ? pct(x.user_probability) : '—'}</td>
                    <td className={cn(td, 'num text-right font-semibold')}>{pct(x.probability)}</td>
                    <td className={td}>
                      {tr('states', x.status)}
                      <div className={cn('text-xs', tone(x.effect) || 'text-muted-foreground')}>{tr('effect', x.effect)}</div>
                    </td>
                    <td className={td}>
                      {x.basis_tr}
                      <Evi items={x.evidence} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-sm text-muted-foreground">{t('theses.review.noTriggers')}</p>
          )}
        </div>
      </Panel>

      <Panel title={t('theses.review.claims')}>
        <div className="overflow-x-auto p-3">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={th}>{t('theses.review.thesisMarker')}</th>
                <th className={th}>{t('theses.review.claim')}</th>
                <th className={th}>{t('theses.review.result')}</th>
                <th className={th}>{t('theses.review.evidence')}</th>
              </tr>
            </thead>
            <tbody>
              {o.claims?.map((c, i) => (
                <tr key={i}>
                  <td className={cn(td, 'font-mono text-xs')}>{c.thesis_markers?.map((m) => `T${m}`).join(' ')}</td>
                  <td className={td}>{c.claim_tr}</td>
                  <td className={cn(td, 'font-semibold', tone(c.verdict))}>{tr('claimVerdict', c.verdict)}</td>
                  <td className={td}>
                    {c.note_tr}
                    <Evi items={c.evidence} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title={t('theses.review.priceAssumes')}>
        <div className="flex flex-col gap-1 p-3 text-sm">
          <p>{o.consensus?.implied_by_price_tr}</p>
          <Evi items={o.consensus?.evidence} />
          <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-muted-foreground">
            <span>{t('theses.review.thesisSectionConsensus')}: {yn(o.consensus?.thesis_section_is_consensus)}</span>
            <span>{t('theses.review.variantDiffers')}: {yn(o.consensus?.variant_differs)}</span>
          </div>
          {o.consensus?.note_tr && <p>{o.consensus.note_tr}</p>}
          <H>{t('theses.review.edgeTitle')}</H>
          <p>
            <b>{tr('edgeCategory', o.edge?.category)}</b> · {t('theses.review.statedInThesis')}: {yn(o.edge?.stated_in_thesis)} · {t('theses.review.accessible')}: {yn(o.edge?.accessible_to_individual)}
          </p>
          <p>{o.edge?.rationale_tr}</p>
          <H>{t('theses.review.counterparty')}</H>
          <p>
            {o.counterparty?.who_tr} — <b>{tr('plausible', o.counterparty?.plausible)}</b>
          </p>
          <p>{o.counterparty?.rationale_tr}</p>
          <Evi items={o.counterparty?.evidence} />
        </div>
      </Panel>

      <Panel title={t('theses.review.killSignals')}>
        <div className="overflow-x-auto p-3">
          {o.kill_signals?.length ? (
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className={th}>{t('theses.review.signal')}</th>
                  <th className={th}>{t('theses.review.metric')}</th>
                  <th className={th}>{t('theses.review.threshold')}</th>
                  <th className={th}>{t('theses.review.now')}</th>
                  <th className={th}>{t('theses.review.state')}</th>
                </tr>
              </thead>
              <tbody>
                {o.kill_signals.map((k, i) => (
                  <tr key={i}>
                    <td className={td}>
                      {k.signal_tr}
                      {k.source === 'agent' && <span className="ml-1 text-xs text-muted-foreground">({t('theses.review.addedByAnalyst')})</span>}
                    </td>
                    <td className={td}>{k.metric}</td>
                    <td className={td}>{k.threshold}</td>
                    <td className={td}>
                      {k.current_value ?? '—'}
                      {k.current_as_of && <span className="ml-1 text-xs text-muted-foreground">{k.current_as_of}</span>}
                      {k.evidence_ref && <div className="font-mono text-xs text-muted-foreground">{k.evidence_ref.slice(0, 80)}</div>}
                    </td>
                    <td className={cn(td, 'font-semibold', tone(k.status))}>{tr('states', k.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-sm text-muted-foreground">{t('theses.review.noKill')}</p>
          )}
          {o.pre_mortem && o.pre_mortem.length > 0 && (
            <>
              <H>{t('theses.review.preMortem')}</H>
              <ul className="list-disc space-y-1 pl-4 text-sm">
                {o.pre_mortem.map((p, i) => (
                  <li key={i}>
                    {p.reason_tr} <span className="text-xs text-muted-foreground">({tr('likelihood', p.likelihood)})</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </Panel>

      <Panel title={t('theses.review.structure')}>
        <div className="p-3">
          <table className="w-full text-sm">
            <tbody>
              {o.structure?.map((s) => (
                <tr key={s.section}>
                  <th className={cn(td, 'text-left font-medium')}>{tr('sections', s.section)}</th>
                  <td className={cn(td, tone(s.status))}>{tr('states', s.status)}</td>
                  <td className={td}>{s.note_tr}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {o.data_gaps && o.data_gaps.length > 0 && (
            <>
              <H>{t('theses.review.dataGaps')}</H>
              <ul className="list-disc space-y-1 pl-4 text-sm">
                {o.data_gaps.map((g, i) => (
                  <li key={i}>{g}</li>
                ))}
              </ul>
            </>
          )}
        </div>
        <RawJson value={output} label={t('theses.review.raw')} />
      </Panel>
    </div>
  )
}
