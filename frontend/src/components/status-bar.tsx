import { Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { type Status, useStatus } from '@/api/status'
import { ageSeconds, formatDuration, lagTone, type Tone, toneClass } from '@/lib/format'
import { cn } from '@/lib/utils'

function Item({ label, tone = 'muted', testId, hash, children }: { label: string; tone?: Tone; testId?: string; hash?: string; children: ReactNode }) {
  return (
    <Link
      to="/system"
      hash={hash}
      className="flex h-full items-center gap-1.5 px-2.5 hover:bg-accent"
      title={label}
    >
      <span className="text-muted-foreground">{label}</span>
      <span data-testid={testId} data-tone={tone} className={toneClass[tone]}>
        {children}
      </span>
    </Link>
  )
}

function overallTone(s: Status): Tone {
  const failed = (s.jobs.failed ?? 0) + (s.jobs.dead ?? 0)
  if (lagTone(s.kap.lag_seconds) === 'bad' || failed > 0 || s.heartbeat_stale || !s.running) return 'bad'
  if (lagTone(s.kap.lag_seconds) === 'warn' || s.governor.paused) return 'warn'
  return 'ok'
}

export function StatusBar() {
  const { t } = useTranslation()
  const { data: s, isError, isPending } = useStatus()

  const wrap = 'flex h-statusbar shrink-0 items-stretch border-t border-border bg-sidebar font-mono text-xs'

  if (isPending || isError || !s) {
    const tone: Tone = isError ? 'bad' : 'muted'
    return (
      <footer className={wrap} data-testid="status-bar">
        <Link to="/system" className="flex items-center gap-1.5 px-2.5 hover:bg-accent">
          <span className={cn('size-1.5 rounded-full bg-current', toneClass[tone])} />
          <span data-testid="status-state" data-tone={tone} className={toneClass[tone]}>
            {isError ? t('status.down') : t('status.loading')}
          </span>
        </Link>
      </footer>
    )
  }

  const lag = s.kap.lag_seconds
  const queued = s.jobs.queued ?? 0
  const running = s.jobs.running ?? 0
  const failed = (s.jobs.failed ?? 0) + (s.jobs.dead ?? 0)
  const backupAge = s.last_backup_at ? ageSeconds(s.last_backup_at, s.server_time) : null
  const hbTone: Tone = s.heartbeat_stale || !s.running ? 'bad' : s.heartbeat_stale === false ? 'ok' : 'muted'
  const overall = overallTone(s)

  return (
    <footer className={wrap} data-testid="status-bar">
      {/* Compact single indicator on mobile */}
      <Link to="/system" className="flex flex-1 items-center gap-2 px-2.5 md:hidden" data-testid="status-compact">
        <span className={cn('size-1.5 rounded-full bg-current', toneClass[overall])} data-tone={overall} />
        <span className={toneClass[lagTone(lag)]}>{lag == null ? '--' : formatDuration(lag)}</span>
        <span className={failed > 0 ? 'text-error' : 'text-muted-foreground'}>
          {failed} {t('status.failed')}
        </span>
        {s.governor.paused && <span className="text-warning">LLM {t('status.llmPaused')}</span>}
      </Link>

      <div className="hidden min-w-0 flex-1 items-stretch divide-x divide-border md:flex">
        <Item label={t('status.lag')} tone={lagTone(lag)} testId="status-lag">
          {lag == null ? '--' : formatDuration(lag)}
        </Item>
        <Link to="/system" className="flex items-center gap-2 px-2.5 hover:bg-accent" title={t('status.jobs')}>
          <span className="text-muted-foreground">{t('status.jobs')}</span>
          <span data-testid="status-jobs-queued">
            {queued} {t('status.queued')}
          </span>
          <span data-testid="status-jobs-running">
            {running} {t('status.running')}
          </span>
          <span data-testid="status-jobs-failed" data-tone={failed > 0 ? 'bad' : 'muted'} className={failed > 0 ? 'text-error' : 'text-muted-foreground'}>
            {failed} {t('status.failed')}
          </span>
        </Link>
        <Item label={t('status.llm')} tone={s.governor.paused ? 'warn' : 'ok'} testId="status-llm" hash="llm">
          {s.governor.paused
            ? `${t('status.llmPaused')}${s.governor.reason ? `: ${s.governor.reason}` : ''}`
            : t('status.llmActive')}
        </Item>
        <Item label={t('status.telegram')} tone={s.telegram.enabled ? (s.telegram.ready ? 'ok' : 'warn') : 'muted'} testId="status-telegram">
          {s.telegram.enabled ? t('status.on') : t('status.off')}
        </Item>
        <Item label={t('status.backup')} testId="status-backup">
          {backupAge == null ? t('status.never') : t('status.ago', { value: formatDuration(backupAge) })}
        </Item>
        <Item label={t('status.heartbeat')} tone={hbTone} testId="status-heartbeat">
          {hbTone === 'bad' ? t('status.heartbeatStale') : hbTone === 'ok' ? t('status.heartbeatOk') : t('status.unknown')}
        </Item>
      </div>
      <span className="hidden items-center px-2.5 text-muted-foreground md:flex" data-testid="status-version">
        papi {s.version}
      </span>
    </footer>
  )
}
