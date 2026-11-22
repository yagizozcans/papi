import { Bell, ChevronDown, Pencil, Plus, Trash2 } from 'lucide-react'
import { DropdownMenu } from 'radix-ui'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { Panel } from '@/components/ui/panel'
import { cn } from '@/lib/utils'
import { ConfirmDialog } from '../shared/confirm-dialog'
import { errorText, PanelError, SkeletonLines } from '../shared/states'
import { ALERT_CHANNELS, type AlertChannel, type AlertRule, useAlertRules, useDeleteRule, useTestNotification, useUpdateRule } from './api'
import { RuleEditor } from './rule-editor'

export function AlertsPanel() {
  const { t } = useTranslation()
  const q = useAlertRules()
  const update = useUpdateRule()
  const del = useDeleteRule()
  const test = useTestNotification()
  const [editing, setEditing] = useState<AlertRule | 'new' | null>(null)
  const [deleting, setDeleting] = useState<AlertRule | null>(null)
  const data = q.data

  const toggle = (r: AlertRule) =>
    update.mutate({ id: r.id, patch: { enabled: !r.enabled } }, { onError: (e) => toast.error(errorText(e) ?? t('alerts.failed')) })

  const sendTest = (channel: AlertChannel) =>
    test.mutate(channel, {
      onSuccess: (r) => (r.ok ? toast.success(t('alerts.testOk', { channel: t(`alerts.channel.${channel}`) })) : toast.error(`${t('alerts.testFailed')}: ${r.detail ?? ''}`)),
      onError: (e) => toast.error(errorText(e) ?? t('alerts.testFailed')),
    })

  const actions = (
    <>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <Button size="sm" variant="ghost" disabled={test.isPending}>
            <Bell />
            {t('alerts.test')}
            <ChevronDown />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content align="end" sideOffset={4} className="z-50 min-w-40 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md">
            {ALERT_CHANNELS.map((c) => (
              <DropdownMenu.Item key={c} onSelect={() => sendTest(c)} className="flex h-7 cursor-default items-center rounded-sm px-2 text-sm outline-none data-[highlighted]:bg-accent">
                {t(`alerts.channel.${c}`)}
              </DropdownMenu.Item>
            ))}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      <Button size="sm" variant="outline" onClick={() => setEditing('new')}>
        <Plus />
        {t('alerts.new')}
      </Button>
    </>
  )

  return (
    <Panel id="alerts" title={t('alerts.title')} actions={actions} className="scroll-mt-3">
      {q.isError && !data ? (
        <PanelError error={q.error} onRetry={() => void q.refetch()} />
      ) : !data ? (
        <SkeletonLines rows={4} />
      ) : data.rules.length === 0 ? (
        <EmptyState title={t('alerts.empty')} className="py-6" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="rules-table">
            <thead>
              <tr className="h-8 border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="px-3 font-normal">{t('alerts.on')}</th>
                <th className="px-3 font-normal">{t('alerts.name')}</th>
                <th className="px-3 font-normal">{t('alerts.filter')}</th>
                <th className="px-3 font-normal">{t('alerts.channels')}</th>
                <th className="px-3 font-normal">{t('alerts.quiet')}</th>
                <th className="px-3" />
              </tr>
            </thead>
            <tbody>
              {data.rules.map((r) => (
                <tr key={r.id} className={cn('h-9 border-b border-border last:border-b-0', !r.enabled && 'opacity-60')} data-testid="rule-row">
                  <td className="px-3">
                    <input type="checkbox" checked={r.enabled} onChange={() => toggle(r)} aria-label={t('alerts.toggle', { name: r.name })} disabled={update.isPending} />
                  </td>
                  <td className="px-3 font-medium">{r.name}</td>
                  <td className="max-w-80 truncate px-3 font-mono text-xs text-muted-foreground" title={r.filter}>
                    {r.filter}
                  </td>
                  <td className="px-3">
                    <span className="flex gap-1">
                      {r.channels.map((c) => (
                        <Badge key={c} variant="outline">
                          {t(`alerts.channel.${c}`)}
                        </Badge>
                      ))}
                    </span>
                  </td>
                  <td className="num px-3 text-xs text-muted-foreground">{r.quiet_hours ? `${r.quiet_hours.start}–${r.quiet_hours.end}` : t('alerts.global')}</td>
                  <td className="px-3 text-right">
                    <Button size="icon" variant="ghost" className="size-7" aria-label={t('alerts.edit', { name: r.name })} onClick={() => setEditing(r)}>
                      <Pencil className="size-3.5" />
                    </Button>
                    <Button size="icon" variant="ghost" className="size-7" aria-label={t('alerts.delete', { name: r.name })} onClick={() => setDeleting(r)}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && !data.telegram_ready && <p className="border-t border-border px-3 py-2 text-xs text-warning">{t('alerts.telegramNotReady')}</p>}

      <RuleEditor key={editing === 'new' ? 'new' : (editing?.id ?? 'none')} open={editing !== null} onOpenChange={(o) => !o && setEditing(null)} rule={editing === 'new' ? null : editing} fields={data?.fields ?? []} />

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={t('alerts.deleteTitle', { name: deleting?.name ?? '' })}
        description={t('alerts.deleteBody')}
        confirmLabel={t('alerts.deleteConfirm')}
        destructive
        pending={del.isPending}
        error={del.isError ? errorText(del.error) : null}
        onConfirm={() => deleting && del.mutate(deleting.id, { onSuccess: () => { toast.success(t('alerts.deleted')); setDeleting(null) } })}
      />
    </Panel>
  )
}
