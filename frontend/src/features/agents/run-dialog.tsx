import { useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { formatNum } from '@/lib/format'
import { errorText } from '../shared/states'
import { useAgentDefs, useStartRun } from './api'

const REF_OK = /^[0-9A-Za-z-]{1,40}$/

/**
 * "Run agent" dialog: pick the agent, confirm the subject (prefilled from context) and the budget,
 * start, and land on the live run page. `agent` / `refValue` prefill; `lockAgent` fixes the agent.
 */
export function RunAgentDialog({
  open,
  onOpenChange,
  agent,
  refValue = '',
  lockAgent,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  agent?: string
  refValue?: string
  lockAgent?: boolean
}) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const defs = useAgentDefs()
  const start = useStartRun()
  const [picked, setPicked] = useState<string | undefined>(undefined)
  const [ref, setRef] = useState<string | null>(null)

  const agents = defs.data ?? []
  const name = picked ?? agent ?? agents[0]?.name ?? ''
  const def = agents.find((a) => a.name === name)
  const refText = ref ?? refValue
  const valid = REF_OK.test(refText.trim())

  const submit = async () => {
    if (!def || !valid) return
    const out = await start.mutateAsync({ agent: def.name, ref: refText.trim() })
    onOpenChange(false)
    void navigate({ to: '/research/agents/runs/$runId', params: { runId: out.run_id } })
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          setPicked(undefined)
          setRef(null)
          start.reset()
        }
        onOpenChange(o)
      }}
    >
      <DialogContent>
        <div className="grid gap-1">
          <DialogTitle>{t('agents.dialog.title')}</DialogTitle>
          <DialogDescription>{t('agents.dialog.desc')}</DialogDescription>
        </div>
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <label className="grid gap-1 text-sm">
            <span className="text-muted-foreground">{t('agents.dialog.agent')}</span>
            <select
              className="h-8 rounded-md border border-border bg-background px-2"
              value={name}
              disabled={lockAgent}
              onChange={(e) => setPicked(e.target.value)}
            >
              {agents.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-sm">
            <span className="text-muted-foreground">{t('agents.dialog.ref')}</span>
            <input
              className="h-8 rounded-md border border-border bg-background px-2 font-mono"
              value={refText}
              placeholder={t(`agents.dialog.ref${def?.ref_kind === 'disclosure' ? 'Disclosure' : def?.ref_kind === 'thesis' ? 'Thesis' : 'Other'}`)}
              maxLength={40}
              onChange={(e) => setRef(e.target.value)}
              aria-invalid={refText !== '' && !valid}
            />
            {refText !== '' && !valid && <span className="text-xs text-error">{t('agents.dialog.invalidRef')}</span>}
          </label>
          {def && (
            <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm" data-testid="run-budget">
              {t('agents.dialog.budget', {
                usd: formatNum(def.max_budget_usd, { decimals: 2, lang: i18n.language }),
                turns: def.max_turns,
                model: def.model,
              })}
              <span className="mt-1 block text-xs text-muted-foreground">{t('agents.dialog.note')}</span>
            </p>
          )}
          {start.isError && <p className="text-sm text-error" role="alert">{errorText(start.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              {t('agents.dialog.cancel')}
            </Button>
            <Button type="submit" disabled={!def || !valid || start.isPending}>
              {start.isPending ? t('agents.dialog.starting') : t('agents.dialog.start')}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
