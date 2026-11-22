import { AlertTriangle, Check, CircleDashed, FileWarning } from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Panel } from '@/components/ui/panel'
import { cn } from '@/lib/utils'
import { errorText } from '../shared/states'
import { type ThesisCheck, type ThesisDetail, useCheckThesis, useSaveThesis } from './api'

const LINE_PX = 20

/** Gutter + textarea editor for the thesis Markdown, with outline, parse errors (clickable lines) and gaps. */
export function ThesisEditor({ detail, onDirtyChange }: { detail: ThesisDetail; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useTranslation()
  const save = useSaveThesis(detail.name)
  const check = useCheckThesis(detail.name)
  const [serverText, setServerText] = useState(detail.text ?? '')
  const [text, setText] = useState(detail.text ?? '')
  const [draftCheck, setDraftCheck] = useState<ThesisCheck | null>(null)
  const [justSaved, setJustSaved] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)
  const gutter = useRef<HTMLDivElement>(null)

  // Adopt a new server text (e.g. the import finished) unless the user is mid-edit.
  const incoming = detail.text ?? ''
  if (incoming !== serverText) {
    if (text === serverText) setText(incoming)
    setServerText(incoming)
  }
  const dirty = text !== serverText
  const reportDirty = (d: boolean) => onDirtyChange?.(d)

  const shown = draftCheck ?? (dirty ? null : detail.check)
  const errorLines = new Set((shown?.errors ?? []).map((e) => e.line).filter((l): l is number => l != null))
  const lineCount = text.split('\n').length

  const jump = (line: number) => {
    const ta = area.current
    if (!ta) return
    const start = text.split('\n').slice(0, line - 1).join('\n').length + (line > 1 ? 1 : 0)
    ta.focus()
    ta.setSelectionRange(start, start)
    ta.scrollTop = Math.max(0, (line - 3) * LINE_PX)
  }

  const doSave = async () => {
    if (!dirty || save.isPending) return
    const d = await save.mutateAsync(text)
    setServerText(d.text ?? text)
    setDraftCheck(null)
    setJustSaved(true)
    reportDirty(false)
  }

  return (
    <Panel
      title={t('theses.editor.title')}
      data-testid="thesis-editor"
      actions={
        <>
          {dirty && <span className="mr-1 text-xs text-warning">{t('theses.editor.dirty')}</span>}
          {!dirty && justSaved && <span className="mr-1 flex items-center gap-1 text-xs text-up"><Check className="size-3" />{t('theses.editor.saved')}</span>}
          <Button size="sm" variant="outline" disabled={check.isPending} onClick={() => void check.mutateAsync(dirty ? text : null).then(setDraftCheck)}>
            {check.isPending ? t('theses.editor.checking') : t('theses.editor.check')}
          </Button>
          <Button size="sm" disabled={!dirty || save.isPending} onClick={() => void doSave()}>
            {save.isPending ? t('theses.editor.saving') : t('theses.editor.save')}
          </Button>
        </>
      }
    >
      <p className="px-3 pt-2 text-xs text-muted-foreground">{t('theses.editor.hint')}</p>
      <div className="grid gap-3 p-3 xl:grid-cols-[minmax(0,1fr)_200px]">
        <div className="flex h-[32rem] min-w-0 overflow-hidden rounded-md border border-border bg-background font-mono text-[13px]" style={{ lineHeight: `${LINE_PX}px` }}>
          <div ref={gutter} aria-hidden="true" className="w-10 shrink-0 select-none overflow-hidden border-r border-border bg-muted/40 py-2 pr-1 text-right text-muted-foreground" data-testid="gutter">
            {Array.from({ length: lineCount }, (_, i) => (
              <div key={i} className={cn(errorLines.has(i + 1) && 'bg-down/20 font-semibold text-down')} style={{ height: LINE_PX }}>
                {i + 1}
              </div>
            ))}
          </div>
          <textarea
            ref={area}
            value={text}
            spellCheck={false}
            wrap="off"
            aria-label={t('theses.editor.ariaLabel')}
            className="h-full min-w-0 flex-1 resize-none overflow-auto whitespace-pre bg-transparent px-2 py-2 outline-none"
            style={{ lineHeight: `${LINE_PX}px` }}
            onChange={(e) => {
              setText(e.target.value)
              setJustSaved(false)
              setDraftCheck(null)
              reportDirty(e.target.value !== serverText)
            }}
            onScroll={(e) => {
              if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop
            }}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                e.preventDefault()
                void doSave()
              }
            }}
          />
        </div>
        <nav aria-label={t('theses.editor.outline')} className="text-sm">
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('theses.editor.outline')}</h3>
          <ul className="flex flex-col gap-0.5">
            {detail.outline.map((o) => (
              <li key={`${o.line}-${o.title}`}>
                <button type="button" className="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-accent" onClick={() => jump(o.line)}>
                  {o.key == null ? <FileWarning className="size-3.5 shrink-0 text-down" /> : o.empty ? <CircleDashed className="size-3.5 shrink-0 text-warning" /> : <Check className="size-3.5 shrink-0 text-up" />}
                  <span className="min-w-0 flex-1 truncate">{o.title}</span>
                  <span className="num text-xs text-muted-foreground">{o.key == null ? t('theses.editor.unknown') : o.empty ? t('theses.editor.empty') : o.items != null ? o.items : ''}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
      </div>

      {(save.isError || check.isError) && <p role="alert" className="px-3 pb-2 text-sm text-error">{errorText(save.error ?? check.error)}</p>}

      {shown && (
        <div className="flex flex-col gap-2 border-t border-border p-3 text-sm" data-testid="thesis-check">
          {shown.ok && (
            <p className="text-xs text-muted-foreground">
              {t('theses.editor.summary', { filled: shown.sections_filled, total: shown.sections_total, triggers: shown.triggers, kills: shown.kill_signals })}
            </p>
          )}
          {shown.errors.length > 0 && (
            <div role="alert" className="rounded-md border border-down/40 bg-down/10 p-2">
              <p className="mb-1 flex items-center gap-1 font-medium text-down">
                <AlertTriangle className="size-4" /> {t('theses.editor.errors')}
              </p>
              <ul className="space-y-1">
                {shown.errors.map((e, i) => (
                  <li key={i} className="flex gap-2">
                    {e.line != null ? (
                      <button type="button" className="num shrink-0 rounded bg-down/20 px-1 text-xs text-down hover:underline" onClick={() => jump(e.line as number)}>
                        {t('theses.editor.line', { n: e.line })}
                      </button>
                    ) : null}
                    <span>{e.message}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {shown.ok &&
            (shown.gaps.length > 0 ? (
              <div className="rounded-md border border-warning/40 bg-warning/10 p-2">
                <p className="mb-1 font-medium text-warning">{t('theses.editor.gaps')}</p>
                <ul className="list-disc space-y-0.5 pl-4">
                  {shown.gaps.map((g, i) => (
                    <li key={i}>{g}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-up">{t('theses.editor.noGaps')}</p>
            ))}
        </div>
      )}
    </Panel>
  )
}
