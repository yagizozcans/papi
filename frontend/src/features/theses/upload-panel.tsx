import { useNavigate } from '@tanstack/react-router'
import { ArrowDown, ArrowUp, FileUp, X } from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Panel } from '@/components/ui/panel'
import { cn } from '@/lib/utils'
import { useAgentDefs } from '../agents/api'
import { errorText } from '../shared/states'
import { useUploadThesis } from './api'

const TICKER = /^[A-Za-z0-9]{3,6}$/
const ext = (name: string) => `.${name.split('.').pop()?.toLowerCase() ?? ''}`

/** Drag-and-drop / file picker for a paper (several files = one thesis, in order). */
export function UploadPanel({ accept }: { accept: string[] }) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const upload = useUploadThesis()
  const defs = useAgentDefs()
  const input = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [over, setOver] = useState(false)
  const [ticker, setTicker] = useState('')
  const [direction, setDirection] = useState('long')
  const [horizon, setHorizon] = useState('')
  const [run, setRun] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const budget = defs.data?.find((a) => a.name === 'strategy-analyst')?.max_budget_usd

  const add = (list: FileList | File[]) => {
    const picked = Array.from(list)
    const bad = picked.find((f) => !accept.includes(ext(f.name)))
    setProblem(bad ? t('theses.uploadPanel.unsupported', { name: bad.name }) : null)
    setFiles((cur) => [...cur, ...picked.filter((f) => accept.includes(ext(f.name)))])
  }
  const move = (i: number, d: number) =>
    setFiles((cur) => {
      const next = [...cur]
      const [f] = next.splice(i, 1)
      if (f) next.splice(i + d, 0, f)
      return next
    })

  const submit = async () => {
    if (files.length === 0) return setProblem(t('theses.uploadPanel.needFiles'))
    if (!TICKER.test(ticker.trim())) return setProblem(t('theses.uploadPanel.needTicker'))
    setProblem(null)
    const out = await upload.mutateAsync({ files, ticker: ticker.trim(), direction, horizon: horizon.trim(), run })
    setFiles([])
    void navigate({ to: '/research/theses/$name', params: { name: out.name } })
  }

  const field = 'h-8 rounded-md border border-border bg-background px-2 text-sm'
  return (
    <Panel title={t('theses.uploadPanel.title')} data-testid="upload-panel">
      <form
        className="flex flex-col gap-3 p-3"
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <div
          role="button"
          tabIndex={0}
          className={cn('flex cursor-pointer flex-col items-center gap-1 rounded-md border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground transition-colors hover:bg-accent/40', over && 'border-brand bg-brand/10')}
          onClick={() => input.current?.click()}
          onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
          onDragOver={(e) => {
            e.preventDefault()
            setOver(true)
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setOver(false)
            add(e.dataTransfer.files)
          }}
        >
          <FileUp className="size-5" />
          <span>{t('theses.uploadPanel.drop')}</span>
          <span className="max-w-xl text-xs">{t('theses.uploadPanel.hint')}</span>
          <input
            ref={input}
            type="file"
            multiple
            hidden
            accept={accept.join(',')}
            aria-label={t('theses.uploadPanel.title')}
            data-testid="upload-input"
            onChange={(e) => {
              if (e.target.files) add(e.target.files)
              e.target.value = ''
            }}
          />
        </div>
        {files.length > 0 && (
          <div>
            <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('theses.uploadPanel.files')}</h4>
            <ol className="divide-y divide-border rounded-md border border-border">
              {files.map((f, i) => (
                <li key={`${f.name}-${i}`} className="flex items-center gap-2 px-2 py-1 text-sm">
                  <span className="num w-5 text-xs text-muted-foreground">{i + 1}</span>
                  <span className="min-w-0 flex-1 truncate">{f.name}</span>
                  <span className="num text-xs text-muted-foreground">{(f.size / 1024).toFixed(0)} KB</span>
                  <Button type="button" size="icon" variant="ghost" className="size-6" disabled={i === 0} onClick={() => move(i, -1)} aria-label={t('theses.uploadPanel.moveUp')}>
                    <ArrowUp />
                  </Button>
                  <Button type="button" size="icon" variant="ghost" className="size-6" disabled={i === files.length - 1} onClick={() => move(i, 1)} aria-label={t('theses.uploadPanel.moveDown')}>
                    <ArrowDown />
                  </Button>
                  <Button type="button" size="icon" variant="ghost" className="size-6" onClick={() => setFiles((c) => c.filter((_, j) => j !== i))} aria-label={t('theses.uploadPanel.remove')}>
                    <X />
                  </Button>
                </li>
              ))}
            </ol>
          </div>
        )}
        <div className="flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t('theses.uploadPanel.ticker')}
            <input className={cn(field, 'w-28 font-mono uppercase text-foreground')} value={ticker} maxLength={6} placeholder="THYAO" onChange={(e) => setTicker(e.target.value)} />
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t('theses.uploadPanel.direction')}
            <select className={cn(field, 'text-foreground')} value={direction} onChange={(e) => setDirection(e.target.value)}>
              <option value="long">long</option>
              <option value="short">short</option>
              <option value="izle">izle</option>
            </select>
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t('theses.uploadPanel.horizon')}
            <input className={cn(field, 'w-36 text-foreground')} value={horizon} maxLength={60} placeholder={t('theses.uploadPanel.horizonPlaceholder')} onChange={(e) => setHorizon(e.target.value)} />
          </label>
          <label className="flex items-center gap-2 pb-1.5 text-sm">
            <input type="checkbox" checked={run} onChange={(e) => setRun(e.target.checked)} />
            {t('theses.uploadPanel.run')}
            {budget != null && <span className="text-xs text-muted-foreground">({t('theses.uploadPanel.runCost', { usd: budget.toLocaleString(i18n.language, { minimumFractionDigits: 2 }) })})</span>}
          </label>
          <Button type="submit" className="ml-auto" disabled={upload.isPending}>
            {upload.isPending ? t('theses.uploadPanel.uploading') : t('theses.uploadPanel.submit')}
          </Button>
        </div>
        {(problem || upload.isError) && (
          <p role="alert" className="text-sm text-error">
            {problem ?? errorText(upload.error)}
          </p>
        )}
      </form>
    </Panel>
  )
}
