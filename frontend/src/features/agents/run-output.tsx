import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { Panel } from '@/components/ui/panel'
import { formatNum } from '@/lib/format'

interface Point {
  text_tr: string
  span_ordinals?: number[]
}
interface Claim {
  kind?: string
  text_tr?: string
  value?: string | number | null
  unit?: string | null
  span_ordinals?: number[]
  citation_ids?: string[]
}
/** kap-analyst structured output (agents/kap-analyst/output.schema.json); every field is optional on purpose. */
export interface KapOutput {
  headline_tr?: string
  event_codes?: string[]
  materiality?: number
  fundamental_direction?: string
  confidence?: number
  summary_tr?: string
  summary_en?: string
  positives?: Point[]
  negatives?: Point[]
  materiality_rationale?: string
  watch_next?: string[]
  claims?: Claim[]
}

function Spans({ p }: { p: { span_ordinals?: number[] } }) {
  if (!p.span_ordinals?.length) return null
  return <span className="ml-1 font-mono text-xs text-muted-foreground">{p.span_ordinals.map((s) => `S${s}`).join(' ')}</span>
}

export function RawJson({ value, label }: { value: unknown; label: string }) {
  return (
    <details className="px-3 py-2">
      <summary className="cursor-pointer text-sm text-muted-foreground">{label}</summary>
      <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted/50 p-2 font-mono text-xs">{JSON.stringify(value, null, 2)}</pre>
    </details>
  )
}

/** Green / red "Çıkarım" block: what the disclosure means for the company, positives and negatives. */
export function Takeaways({ positives, negatives }: { positives?: Point[]; negatives?: Point[] }) {
  const { t } = useTranslation()
  const col = (tone: 'up' | 'down', title: string, items: Point[] | undefined, empty: string) => (
    <div
      className={tone === 'up' ? 'rounded-md border border-up/40 bg-up/10 p-3' : 'rounded-md border border-down/40 bg-down/10 p-3'}
      data-testid={tone === 'up' ? 'takeaways-positive' : 'takeaways-negative'}
    >
      <h4 className={tone === 'up' ? 'mb-1 text-sm font-semibold text-up' : 'mb-1 text-sm font-semibold text-down'}>{title}</h4>
      {items && items.length > 0 ? (
        <ul className="list-disc space-y-1 pl-4 text-sm">
          {items.map((p, i) => (
            <li key={i}>
              {p.text_tr}
              <Spans p={p} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{empty}</p>
      )}
    </div>
  )
  return (
    <div className="grid gap-2 md:grid-cols-2">
      {col('up', t('agents.output.positives'), positives, t('agents.output.noPositives'))}
      {col('down', t('agents.output.negatives'), negatives, t('agents.output.noNegatives'))}
    </div>
  )
}

function KapAnswer({ o }: { o: KapOutput }) {
  const { t, i18n } = useTranslation()
  const dir = o.fundamental_direction
  return (
    <Panel title={t('agents.output.result')} data-testid="kap-output">
      <div className="flex flex-col gap-3 p-3">
        {o.headline_tr && <p className="text-base font-semibold leading-snug">{o.headline_tr}</p>}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {o.event_codes && o.event_codes.length > 0 && (
            <span className="text-muted-foreground">
              {t('agents.output.events')}: <span className="font-mono">{o.event_codes.join(', ')}</span>
            </span>
          )}
          {o.materiality != null && <Badge variant="outline">{t('agents.output.materiality')} {o.materiality}</Badge>}
          {dir && (
            <Badge variant={dir === 'positive' ? 'up' : dir === 'negative' ? 'down' : 'outline'}>
              {t('agents.output.direction')}: {t(`agents.output.directions.${dir}`, { defaultValue: dir })}
            </Badge>
          )}
          {o.confidence != null && <Badge variant="outline">{t('agents.output.confidence')} {formatNum(o.confidence, { decimals: 2, lang: i18n.language })}</Badge>}
        </div>
        {o.summary_tr && (
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('agents.output.summary')}</h3>
            <p className="mt-1 text-sm">{o.summary_tr}</p>
            {o.summary_en && <p className="mt-1 text-sm text-muted-foreground">{o.summary_en}</p>}
          </div>
        )}
        <div>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('agents.output.takeaways')}</h3>
          <Takeaways positives={o.positives} negatives={o.negatives} />
        </div>
        {o.materiality_rationale && (
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('agents.output.rationale')}</h3>
            <p className="mt-1 text-sm">{o.materiality_rationale}</p>
          </div>
        )}
        {o.watch_next && o.watch_next.length > 0 && (
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('agents.output.watchNext')}</h3>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-sm">
              {o.watch_next.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        )}
        {o.claims && o.claims.length > 0 && (
          <div>
            <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('agents.output.claims', { count: o.claims.length })}</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-3 font-medium">{t('agents.output.claimKind')}</th>
                    <th className="py-1 pr-3 font-medium">{t('agents.output.claimText')}</th>
                    <th className="py-1 pr-3 font-medium">{t('agents.output.claimValue')}</th>
                    <th className="py-1 font-medium">{t('agents.output.claimSource')}</th>
                  </tr>
                </thead>
                <tbody>
                  {o.claims.map((c, i) => (
                    <tr key={i} className="border-t border-border align-top">
                      <td className="py-1 pr-3 text-muted-foreground">{c.kind}</td>
                      <td className="py-1 pr-3">{c.text_tr}</td>
                      <td className="num py-1 pr-3 whitespace-nowrap">{[c.value, c.unit].filter((v) => v != null && v !== '').join(' ')}</td>
                      <td className="py-1 font-mono text-xs text-muted-foreground">
                        {[...(c.span_ordinals ?? []).map((s) => `S${s}`), ...(c.citation_ids ?? [])].join(' ')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
      <RawJson value={o} label={t('agents.output.raw')} />
    </Panel>
  )
}

/** The final answer of a run: kap-analyst gets the rich view, anything else its text or raw JSON. */
export function RunAnswer({ agent, output, text, review }: { agent: string; output: Record<string, unknown> | null | undefined; text: string | null | undefined; review: (o: Record<string, unknown>) => React.ReactNode }) {
  const { t } = useTranslation()
  if (output && agent === 'strategy-analyst') return <>{review(output)}</>
  if (output && 'positives' in output) return <KapAnswer o={output as KapOutput} />
  if (output) {
    return (
      <Panel title={t('agents.output.result')}>
        <RawJson value={output} label={t('agents.output.raw')} />
      </Panel>
    )
  }
  if (text) {
    return (
      <Panel title={t('agents.output.answer')}>
        <pre className="whitespace-pre-wrap p-3 font-mono text-xs">{text}</pre>
      </Panel>
    )
  }
  return null
}
