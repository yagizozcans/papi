import type { TFunction } from 'i18next'
import { Download, ExternalLink } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { DocumentView } from '@/components/kit/document-view'
import { Panel } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { EventDetail } from './api'

type Doc = EventDetail['documents'][number]

function sizeLabel(bytes: number | null): string {
  if (bytes == null) return ''
  return bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`
}

export function docLabel(doc: Doc, t: TFunction): string {
  const kind = t(`feed.documents.kind.${doc.kind}`, { defaultValue: doc.kind })
  return doc.kind === 'attachment' && doc.filename ? `${kind}: ${doc.filename}` : kind
}

/** Body and attachments as tabs. `value`/`onValueChange` are controlled so citations can switch tabs. */
export function Documents({
  detail,
  value,
  onValueChange,
  highlight,
}: {
  detail: EventDetail
  value: string
  onValueChange: (v: string) => void
  highlight: string | null
}) {
  const { t } = useTranslation()
  const docs = detail.documents
  return (
    <Panel title={t('feed.sections.documents')} aria-label={t('feed.sections.documents')}>
      {docs.length === 0 ? (
        <p className="p-3 text-sm text-muted-foreground">
          {t('feed.documents.none')}{' '}
          {detail.item.kap_url && (
            <a href={detail.item.kap_url} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">
              {t('feed.documents.readOnKap')}
            </a>
          )}
        </p>
      ) : (
        <Tabs value={value} onValueChange={onValueChange}>
          <TabsList className="px-3">
            {docs.map((d) => (
              <TabsTrigger key={d.id} value={d.id} className="max-w-60">
                <span className="truncate">{docLabel(d, t)}</span>
              </TabsTrigger>
            ))}
          </TabsList>
          {docs.map((d) => {
            const unplaced = new Set(d.unplaced_span_ids)
            const rest = d.spans.filter((s) => unplaced.has(s.id))
            return (
              <TabsContent key={d.id} value={d.id} className="p-3">
                <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  {d.mime && <span>{d.mime}</span>}
                  {d.byte_size != null && <span className="num">{sizeLabel(d.byte_size)}</span>}
                  {d.download_url && (
                    <a href={d.download_url} target="_blank" rel="noopener noreferrer" download className="inline-flex items-center gap-1 text-brand hover:underline">
                      <Download className="size-3" /> {t('feed.documents.download')}
                    </a>
                  )}
                  {d.raw_url && (
                    <a href={d.raw_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
                      <ExternalLink className="size-3" /> {t('feed.documents.source')}
                    </a>
                  )}
                </div>
                {d.text_length > 0 || d.html ? (
                  <DocumentView html={d.html} highlightAnchor={highlight} />
                ) : (
                  <p className="text-sm text-muted-foreground">{t('feed.documents.empty')}</p>
                )}
                {rest.length > 0 && (
                  <ol className="mt-3 flex flex-col gap-1 border-t border-border pt-3 text-sm">
                    {rest.map((s) => (
                      <li key={s.id}>
                        <b className="font-mono text-xs">[S{s.ordinal}]</b>
                        {s.page != null && <small className="ml-1 text-muted-foreground">s.{s.page}</small>} {s.text}
                      </li>
                    ))}
                  </ol>
                )}
              </TabsContent>
            )
          })}
        </Tabs>
      )}
    </Panel>
  )
}
