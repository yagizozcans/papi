import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { TickerChip } from '@/components/kit/ticker-chip'
import { Button } from '@/components/ui/button'
import { parseDecimal } from '@/lib/parse-number'
import { ConfirmDialog } from '../shared/confirm-dialog'
import { Input } from '../shared/form-controls'
import { errorText } from '../shared/states'
import { type PendingAction, useApplyPending, useDismissPending } from './actions'

const proposalText = (a: PendingAction): string | null => {
  const text = (a.proposal as { text_tr?: unknown } | null)?.text_tr
  return typeof text === 'string' ? text : null
}
const proposalQty = (a: PendingAction): string => {
  const q = (a.proposal as { quantity?: unknown } | null)?.quantity
  return typeof q === 'string' || typeof q === 'number' ? String(q) : ''
}

/** One pending corporate action with Apply / Dismiss; each opens a confirm dialog showing the proposal. */
export function PendingRow({ action: a }: { action: PendingAction }) {
  const { t } = useTranslation()
  const [mode, setMode] = useState<'apply' | 'dismiss' | null>(null)
  const [qty, setQty] = useState('')
  const [qtyError, setQtyError] = useState(false)
  const apply = useApplyPending()
  const dismiss = useDismissPending()
  const text = proposalText(a)

  const open = (m: 'apply' | 'dismiss') => {
    setQty(proposalQty(a))
    setQtyError(false)
    apply.reset()
    dismiss.reset()
    setMode(m)
  }
  const close = () => setMode(null)

  const onApply = () => {
    const parsed = parseDecimal(qty)
    const original = parseDecimal(proposalQty(a))
    if (qty.trim() && (parsed == null || parsed <= 0)) return setQtyError(true)
    const override = parsed != null && parsed !== original ? parsed : null
    apply.mutate(
      { id: a.id, quantityOverride: override },
      {
        onSuccess: () => {
          toast.success(t('portfolio.pending.applied'))
          close()
        },
      },
    )
  }
  const onDismiss = () =>
    dismiss.mutate(a.id, {
      onSuccess: () => {
        toast.success(t('portfolio.pending.dismissed'))
        close()
      },
    })

  return (
    <>
      <div className="flex flex-col gap-1.5" data-testid="pending-action">
        <div className="flex items-center gap-2">
          {a.ticker && <TickerChip ticker={a.ticker} />}
          <span className="text-sm">{a.kind}</span>
          {a.ex_date && <span className="num ml-auto text-xs text-muted-foreground">{a.ex_date}</span>}
        </div>
        {text && <p className="text-xs text-muted-foreground">{text}</p>}
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => open('apply')}>
            {t('portfolio.pending.apply')}
          </Button>
          <Button size="sm" variant="outline" onClick={() => open('dismiss')}>
            {t('portfolio.pending.dismiss')}
          </Button>
        </div>
      </div>

      <ConfirmDialog
        open={mode === 'apply'}
        onOpenChange={(o) => !o && close()}
        title={t('portfolio.pending.applyTitle', { ticker: a.ticker ?? '', kind: a.kind })}
        description={text ?? undefined}
        confirmLabel={t('portfolio.pending.apply')}
        pending={apply.isPending}
        error={apply.isError ? errorText(apply.error) : null}
        onConfirm={onApply}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor={`pa-qty-${a.id}`} className="text-xs font-medium text-muted-foreground">
            {t('portfolio.pending.quantity')}
          </label>
          <Input id={`pa-qty-${a.id}`} inputMode="decimal" value={qty} aria-invalid={qtyError} onChange={(e) => setQty(e.target.value)} />
          <p className="text-xs text-muted-foreground">{t('portfolio.pending.quantityHint')}</p>
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={mode === 'dismiss'}
        onOpenChange={(o) => !o && close()}
        title={t('portfolio.pending.dismissTitle', { ticker: a.ticker ?? '', kind: a.kind })}
        description={text ?? t('portfolio.pending.dismissBody')}
        confirmLabel={t('portfolio.pending.dismiss')}
        destructive
        pending={dismiss.isPending}
        error={dismiss.isError ? errorText(dismiss.error) : null}
        onConfirm={onDismiss}
      />
    </>
  )
}
