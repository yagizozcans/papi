import { zodResolver } from '@hookform/resolvers/zod'
import { Check, X } from 'lucide-react'
import { useForm, useWatch } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { z } from 'zod'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Field, Input } from '../shared/form-controls'
import { errorText } from '../shared/states'
import { ALERT_CHANNELS, type AlertRule, type AlertRuleIn, useCreateRule, useUpdateRule, useValidateFilter } from './api'

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/

export const ruleSchema = z
  .object({
    name: z.string().trim().min(1, 'alerts.err.name'),
    filter: z.string().trim().min(1, 'alerts.err.filter'),
    channels: z.array(z.enum(['telegram', 'macos'])).min(1, 'alerts.err.channels'),
    customQuiet: z.boolean(),
    quietStart: z.string(),
    quietEnd: z.string(),
    enabled: z.boolean(),
  })
  .superRefine((v, ctx) => {
    if (!v.customQuiet) return
    if (!HHMM.test(v.quietStart)) ctx.addIssue({ code: 'custom', path: ['quietStart'], message: 'alerts.err.time' })
    if (!HHMM.test(v.quietEnd)) ctx.addIssue({ code: 'custom', path: ['quietEnd'], message: 'alerts.err.time' })
  })
export type RuleForm = z.infer<typeof ruleSchema>

export function toRuleIn(v: RuleForm): AlertRuleIn {
  return {
    name: v.name.trim(),
    filter: v.filter.trim(),
    channels: v.channels,
    quiet_hours: v.customQuiet ? { start: v.quietStart, end: v.quietEnd } : null,
    enabled: v.enabled,
  }
}

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Rule being edited; null = new rule. */
  rule: AlertRule | null
  fields: string[]
}

export function RuleEditor({ open, onOpenChange, rule, fields }: Props) {
  const { t } = useTranslation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="top-[8%] max-w-xl" aria-describedby="rule-desc">
        <div className="flex flex-col gap-1">
          <DialogTitle>{rule ? t('alerts.editTitle') : t('alerts.newTitle')}</DialogTitle>
          <DialogDescription id="rule-desc">{t('alerts.editorHint')}</DialogDescription>
        </div>
        {open && <RuleForm rule={rule} fields={fields} onDone={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  )
}

function RuleForm({ rule, fields, onDone }: { rule: AlertRule | null; fields: string[]; onDone: () => void }) {
  const { t } = useTranslation()
  const create = useCreateRule()
  const update = useUpdateRule()
  const validate = useValidateFilter()
  const {
    register,
    handleSubmit,
    setValue,
    getValues,
    control,
    formState: { errors },
  } = useForm<RuleForm>({
    resolver: zodResolver(ruleSchema),
    defaultValues: {
      name: rule?.name ?? '',
      filter: rule?.filter ?? '',
      channels: rule?.channels ?? ['telegram', 'macos'],
      customQuiet: !!rule?.quiet_hours,
      quietStart: rule?.quiet_hours?.start ?? '23:00',
      quietEnd: rule?.quiet_hours?.end ?? '07:30',
      enabled: rule?.enabled ?? true,
    },
  })
  const channels = useWatch({ control, name: 'channels' })
  const customQuiet = useWatch({ control, name: 'customQuiet' })
  const err = (m?: string) => (m ? t(m) : undefined)
  const mutation = rule ? update : create

  const onSubmit = handleSubmit((v) => {
    const body = toRuleIn(v)
    const done = () => {
      toast.success(t(rule ? 'alerts.updated' : 'alerts.created', { name: body.name }))
      onDone()
    }
    if (rule) update.mutate({ id: rule.id, patch: body }, { onSuccess: done })
    else create.mutate(body, { onSuccess: done })
  })

  const toggleChannel = (c: (typeof ALERT_CHANNELS)[number]) =>
    setValue('channels', channels.includes(c) ? channels.filter((x) => x !== c) : [...channels, c], { shouldValidate: true })

  return (
    <form onSubmit={(e) => void onSubmit(e)} className="flex flex-col gap-3" noValidate>
      <Field label={t('alerts.name')} htmlFor="rule-name" error={err(errors.name?.message)}>
        <Input id="rule-name" aria-invalid={!!errors.name} {...register('name')} />
      </Field>
      <Field label={t('alerts.filter')} htmlFor="rule-filter" error={err(errors.filter?.message)} hint={`${t('alerts.fields')}: ${fields.join(', ')}`}>
        <textarea
          id="rule-filter"
          rows={3}
          spellCheck={false}
          aria-invalid={!!errors.filter}
          placeholder={'scope = "holdings" AND materiality >= 60'}
          className="w-full rounded-md border border-input bg-transparent px-2 py-1.5 font-mono text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring aria-[invalid=true]:border-error"
          {...register('filter')}
        />
      </Field>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={validate.isPending || !getValues('filter').trim()}
          onClick={() => validate.mutate(getValues('filter'))}
        >
          {t('alerts.validate')}
        </Button>
        {validate.data?.ok && (
          <span className="inline-flex items-center gap-1 text-up" data-testid="filter-ok">
            <Check className="size-3.5" />
            {t('alerts.filterOk')}
          </span>
        )}
        {validate.data && !validate.data.ok && (
          <span className="inline-flex items-center gap-1 text-error" role="alert">
            <X className="size-3.5" />
            {validate.data.error}
          </span>
        )}
      </div>

      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs font-medium text-muted-foreground">{t('alerts.channels')}</legend>
        <div className="flex gap-4">
          {ALERT_CHANNELS.map((c) => (
            <label key={c} className="flex items-center gap-1.5 text-sm">
              <input type="checkbox" checked={channels.includes(c)} onChange={() => toggleChannel(c)} />
              {t(`alerts.channel.${c}`)}
            </label>
          ))}
        </div>
        {errors.channels && (
          <p role="alert" className="text-xs text-error">
            {err(errors.channels.message)}
          </p>
        )}
      </fieldset>

      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-sm">
          <input type="checkbox" {...register('customQuiet')} />
          {t('alerts.customQuiet')}
        </label>
        {customQuiet && (
          <>
            <Field label={t('alerts.quietStart')} htmlFor="rule-qs" error={err(errors.quietStart?.message)} className="w-28">
              <Input id="rule-qs" type="time" aria-invalid={!!errors.quietStart} {...register('quietStart')} />
            </Field>
            <Field label={t('alerts.quietEnd')} htmlFor="rule-qe" error={err(errors.quietEnd?.message)} className="w-28">
              <Input id="rule-qe" type="time" aria-invalid={!!errors.quietEnd} {...register('quietEnd')} />
            </Field>
          </>
        )}
        {!customQuiet && <span className="text-xs text-muted-foreground">{t('alerts.globalQuiet')}</span>}
      </div>

      <label className="flex items-center gap-1.5 text-sm">
        <input type="checkbox" {...register('enabled')} />
        {t('alerts.enabled')}
      </label>

      {mutation.isError && (
        <p role="alert" className="rounded-md border border-error/40 bg-error/10 px-2 py-1.5 text-sm text-error">
          {errorText(mutation.error)}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onDone}>
          {t('portfolio.common.cancel')}
        </Button>
        <Button type="submit" disabled={mutation.isPending}>
          {t('alerts.save')}
        </Button>
      </div>
    </form>
  )
}
