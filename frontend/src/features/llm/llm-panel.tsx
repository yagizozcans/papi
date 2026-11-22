import { ChevronDown, CirclePause, CirclePlay } from 'lucide-react'
import { DropdownMenu } from 'radix-ui'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Panel } from '@/components/ui/panel'
import { formatDateTime, formatNum, formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'
import { errorText, PanelError, SkeletonLines } from '../shared/states'
import { type LlmStatus, type PauseOption, PAUSE_OPTIONS, useLlmStatus, usePauseLlm, useResumeLlm } from './api'

function UsageBar({ label, used, cap, testId }: { label: string; used: number; cap: number; testId: string }) {
  const ratio = cap > 0 ? used / cap : 0
  const tone = ratio >= 1 ? 'bg-error' : ratio >= 0.8 ? 'bg-warning' : 'bg-brand'
  return (
    <div className="flex flex-col gap-1" data-testid={testId}>
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span>{label}</span>
        <span className="num text-xs text-muted-foreground">
          {formatNum(used, { decimals: 3 })} / {formatNum(cap, { decimals: 2 })} · {formatPercent(ratio * 100, { decimals: 0 })}
        </span>
      </div>
      <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, Math.round(ratio * 100))} className="h-2 overflow-hidden rounded-sm bg-muted">
        <div className={cn('h-full rounded-sm', tone)} style={{ width: `${Math.min(100, ratio * 100)}%` }} />
      </div>
    </div>
  )
}

export function LlmControls({ status }: { status: LlmStatus }) {
  const { t } = useTranslation()
  const pause = usePauseLlm()
  const resume = useResumeLlm()

  const doPause = (o: PauseOption) =>
    pause.mutate(
      { minutes: o.minutes },
      { onSuccess: () => toast.success(t('llm.pausedToast')), onError: (e) => toast.error(errorText(e) ?? t('llm.failed')) },
    )
  const doResume = () =>
    resume.mutate(undefined, { onSuccess: () => toast.success(t('llm.resumedToast')), onError: (e) => toast.error(errorText(e) ?? t('llm.failed')) })

  if (status.paused) {
    return (
      <Button size="sm" onClick={doResume} disabled={resume.isPending}>
        <CirclePlay />
        {t('llm.resume')}
      </Button>
    )
  }
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button size="sm" variant="outline" disabled={pause.isPending}>
          <CirclePause />
          {t('llm.pause')}
          <ChevronDown />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" sideOffset={4} className="z-50 min-w-44 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md">
          {PAUSE_OPTIONS.map((o) => (
            <DropdownMenu.Item key={o.key} onSelect={() => doPause(o)} className="flex h-7 cursor-default items-center rounded-sm px-2 text-sm outline-none data-[highlighted]:bg-accent">
              {t(`llm.duration.${o.key}`)}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}

export function LlmPanel() {
  const { t } = useTranslation()
  const q = useLlmStatus()
  const s = q.data

  return (
    <Panel id="llm" title={t('llm.title')} actions={s && <LlmControls status={s} />} className="scroll-mt-3">
      {q.isError && !s ? (
        <PanelError error={q.error} onRetry={() => void q.refetch()} />
      ) : !s ? (
        <SkeletonLines rows={5} />
      ) : (
        <div className="flex flex-col gap-3 p-3" data-testid="llm-panel">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {s.paused ? (
              <Badge variant="warning" data-testid="llm-state">
                {t('llm.paused')}
              </Badge>
            ) : (
              <Badge variant="up" data-testid="llm-state">
                {t('llm.active')}
              </Badge>
            )}
            {s.paused && (
              <span className="text-muted-foreground">
                {s.paused_until ? t('llm.until', { time: formatDateTime(s.paused_until, { seconds: false }) }) : t('llm.untilResumed')}
                {s.pause_reason ? ` · ${s.pause_reason}` : ''}
              </span>
            )}
            {s.calibration_mode && <Badge variant="outline">{t('llm.calibration')}</Badge>}
            {s.focus && <Badge variant="outline">{t('llm.focus', { priority: s.focus.max_priority, time: formatDateTime(s.focus.until, { seconds: false }) })}</Badge>}
            {s.night_window_active && <Badge variant="outline">{t('llm.night')}</Badge>}
          </div>

          <UsageBar testId="usage-5h" label={t('llm.use5h')} used={s.used_5h} cap={s.cap_5h} />
          <UsageBar testId="usage-chat" label={t('llm.useChat')} used={s.used_chat_5h} cap={s.cap_chat_5h} />
          <UsageBar testId="usage-week" label={t('llm.useWeek')} used={s.used_week} cap={s.cap_week} />

          {s.subscription_bars && (
            <div className="text-xs text-muted-foreground" data-testid="llm-sub-bars">
              {([['five_hour', 'llm.sub5h'], ['seven_day', 'llm.subWeek']] as const).map(([k, label]) => {
                const b = s.subscription_bars?.[k]
                return b?.utilization != null ? (
                  <div key={k}>
                    {t(label)}: {formatPercent(b.utilization * 100, { decimals: 0 })}
                    {b.resets_at ? ` · ${t('llm.resets', { time: formatDateTime(b.resets_at, { seconds: false }) })}` : ''}
                  </div>
                ) : null
              })}
            </div>
          )}

          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
            <span>{t('llm.concurrency', { n: s.concurrency })}</span>
            <span data-testid="llm-queue">{t('llm.queue', { n: s.queue.deferred_total })}</span>
            {Object.entries(s.queue.deferred).map(([k, n]) => (
              <span key={k} className="num">
                {k}: {n}
              </span>
            ))}
          </div>

          {s.by_task.length > 0 && (
            <table className="w-full text-xs">
              <caption className="mb-1 text-left font-medium uppercase tracking-wide text-muted-foreground">{t('llm.byTask')}</caption>
              <thead>
                <tr className="text-muted-foreground">
                  <th className="text-left font-normal">{t('llm.task')}</th>
                  <th className="num text-right font-normal">{t('llm.calls')}</th>
                  <th className="num text-right font-normal">{t('llm.cost')}</th>
                  <th className="num text-right font-normal">{t('llm.failures')}</th>
                </tr>
              </thead>
              <tbody>
                {s.by_task.map((r) => (
                  <tr key={r.task} className="border-t border-border">
                    <td className="py-1 font-mono">{r.task}</td>
                    <td className="num text-right">{r.calls}</td>
                    <td className="num text-right">{formatNum(r.cost, { decimals: 3 })}</td>
                    <td className={cn('num text-right', r.failures > 0 && 'text-error')}>{r.failures}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </Panel>
  )
}
