import { zodResolver } from '@hookform/resolvers/zod'
import { TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { z } from 'zod'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { formatMoney, formatNum, formatPercent } from '@/lib/format'
import { parseDecimal } from '@/lib/parse-number'
import { errorText } from '../shared/states'
import { Field, Input, Select } from '../shared/form-controls'
import { TickerCombobox } from '../shared/ticker-combobox'
import { type PositionSnap, type TxnIn, type TxnResult, useRecordTrade, useTradePreview } from './actions'

export const TRADE_TYPES = ['buy', 'sell', 'opening_balance'] as const

const positive = (s: string) => {
  const n = parseDecimal(s)
  return n != null && n > 0
}
const nonNegative = (s: string) => {
  const n = parseDecimal(s)
  return n != null && n >= 0
}

/** Form values stay strings (what the user typed); `toPayload` parses the Turkish/English decimals. */
export const tradeSchema = z.object({
  type: z.enum(TRADE_TYPES),
  ticker: z.string().trim().min(1, 'portfolio.trade.err.ticker'),
  quantity: z.string().refine(positive, 'portfolio.trade.err.quantity'),
  price: z.string().refine(nonNegative, 'portfolio.trade.err.price'),
  fees: z.string().refine((s) => s.trim() === '' || nonNegative(s), 'portfolio.trade.err.fees'),
  date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'portfolio.trade.err.date'),
  note: z.string().max(500),
})
export type TradeForm = z.infer<typeof tradeSchema>

export function todayIst(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Istanbul' }).format(new Date())
}

export function toPayload(v: TradeForm): TxnIn {
  return {
    type: v.type,
    ticker: v.ticker.trim().toUpperCase(),
    quantity: parseDecimal(v.quantity)!,
    price: parseDecimal(v.price)!,
    fees: v.fees.trim() ? parseDecimal(v.fees)! : 0,
    trade_date: v.date,
    note: v.note.trim() || null,
  }
}

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  defaultTicker?: string
}

export function TradeDialog({ open, onOpenChange, defaultTicker }: Props) {
  const { t } = useTranslation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="top-[8%] max-w-lg" aria-describedby="trade-desc">
        <div className="flex flex-col gap-1">
          <DialogTitle>{t('portfolio.trade.title')}</DialogTitle>
          <DialogDescription id="trade-desc">{t('portfolio.trade.description')}</DialogDescription>
        </div>
        {open && <TradeFormBody defaultTicker={defaultTicker} onDone={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  )
}

function Snap({ label, s }: { label: string; s: PositionSnap | null | undefined }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col gap-0.5 rounded-md border border-border p-2" data-testid={`snap-${label}`}>
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t(`portfolio.trade.${label}`)}</span>
      <span className="num text-sm" data-testid={`snap-${label}-qty`}>
        {t('portfolio.holdings.qty')}: {formatNum(s?.quantity ?? 0, { decimals: Number.isInteger(s?.quantity ?? 0) ? 0 : 4 })}
      </span>
      <span className="num text-sm">
        {t('portfolio.holdings.avgCost')}: {formatNum(s?.avg_cost)}
      </span>
      <span className="num text-sm">
        {t('portfolio.holdings.weight')}: {s?.weight == null ? '–' : formatPercent(s.weight * 100, { decimals: 1 })}
      </span>
    </div>
  )
}

function TradeFormBody({ defaultTicker, onDone }: { defaultTicker?: string; onDone: () => void }) {
  const { t } = useTranslation()
  const [preview, setPreview] = useState<{ payload: TxnIn; result: TxnResult } | null>(null)
  const previewMut = useTradePreview()
  const recordMut = useRecordTrade()
  const {
    register,
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<TradeForm>({
    resolver: zodResolver(tradeSchema),
    defaultValues: { type: 'buy', ticker: defaultTicker ?? '', quantity: '', price: '', fees: '', date: todayIst(), note: '' },
  })
  const err = (m?: string) => (m ? t(m) : undefined)

  const onPreview = handleSubmit((v) => {
    const payload = toPayload(v)
    previewMut.mutate(payload, { onSuccess: (result) => setPreview({ payload, result }) })
  })

  const onConfirm = () => {
    if (!preview) return
    recordMut.mutate(preview.payload, {
      onSuccess: (r) => {
        toast.success(t('portfolio.trade.recorded', { type: t(`portfolio.txns.types.${r.type}`, { defaultValue: r.type }), ticker: r.ticker ?? '' }))
        onDone()
      },
    })
  }

  if (preview) {
    const r = preview.result
    return (
      <div className="flex flex-col gap-3" data-testid="trade-preview">
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium">{t(`portfolio.txns.types.${r.type}`, { defaultValue: r.type })}</span>
          <span className="font-mono font-semibold">{r.ticker}</span>
          {r.name && <span className="text-muted-foreground">{r.name}</span>}
        </div>
        <div className="num text-sm" data-testid="preview-line">
          {formatNum(r.quantity, { decimals: Number.isInteger(r.quantity) ? 0 : 4 })} × {formatNum(r.price)}
          {r.gross != null && <> = {formatMoney(r.gross)}</>}
          {r.fees > 0 && <span className="text-muted-foreground"> · {t('portfolio.txns.fees')} {formatNum(r.fees)}</span>}
          <span className="text-muted-foreground"> · {r.trade_date}</span>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <Snap label="before" s={r.before} />
          <Snap label="after" s={r.after} />
        </div>
        {r.warnings.length > 0 && (
          <ul role="status" className="flex flex-col gap-1 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-sm text-warning" data-testid="trade-warnings">
            {r.warnings.map((w) => (
              <li key={w} className="flex items-start gap-1.5">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
                {w}
              </li>
            ))}
          </ul>
        )}
        {recordMut.isError && (
          <p role="alert" className="text-sm text-error">
            {errorText(recordMut.error)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setPreview(null)} disabled={recordMut.isPending}>
            {t('portfolio.trade.back')}
          </Button>
          <Button onClick={onConfirm} disabled={recordMut.isPending}>
            {t('portfolio.trade.confirm')}
          </Button>
        </div>
      </div>
    )
  }

  return (
    <form onSubmit={(e) => void onPreview(e)} className="flex flex-col gap-3" noValidate>
      <div className="grid grid-cols-2 gap-3">
        <Field label={t('portfolio.trade.type')} htmlFor="trade-type">
          <Select id="trade-type" {...register('type')}>
            {TRADE_TYPES.map((k) => (
              <option key={k} value={k}>
                {t(`portfolio.txns.types.${k}`)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t('portfolio.trade.ticker')} htmlFor="trade-ticker" error={err(errors.ticker?.message)}>
          <Controller control={control} name="ticker" render={({ field }) => <TickerCombobox id="trade-ticker" value={field.value} onChange={field.onChange} invalid={!!errors.ticker} placeholder="THYAO" />} />
        </Field>
        <Field label={t('portfolio.trade.quantity')} htmlFor="trade-qty" error={err(errors.quantity?.message)}>
          <Input id="trade-qty" inputMode="decimal" placeholder="100" aria-invalid={!!errors.quantity} {...register('quantity')} />
        </Field>
        <Field label={t('portfolio.trade.price')} htmlFor="trade-price" error={err(errors.price?.message)}>
          <Input id="trade-price" inputMode="decimal" placeholder="285,40" aria-invalid={!!errors.price} {...register('price')} />
        </Field>
        <Field label={t('portfolio.trade.date')} htmlFor="trade-date" error={err(errors.date?.message)}>
          <Input id="trade-date" type="date" aria-invalid={!!errors.date} {...register('date')} />
        </Field>
        <Field label={t('portfolio.trade.fees')} htmlFor="trade-fees" error={err(errors.fees?.message)}>
          <Input id="trade-fees" inputMode="decimal" placeholder="0,00" aria-invalid={!!errors.fees} {...register('fees')} />
        </Field>
      </div>
      <Field label={t('portfolio.trade.note')} htmlFor="trade-note">
        <Input id="trade-note" {...register('note')} />
      </Field>
      {previewMut.isError && (
        <p role="alert" className="rounded-md border border-error/40 bg-error/10 px-2 py-1.5 text-sm text-error">
          {errorText(previewMut.error)}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onDone}>
          {t('portfolio.common.cancel')}
        </Button>
        <Button type="submit" disabled={previewMut.isPending}>
          {t('portfolio.trade.preview')}
        </Button>
      </div>
    </form>
  )
}
