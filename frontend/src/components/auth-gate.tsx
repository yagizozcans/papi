import { KeyRound } from 'lucide-react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { isUnauthorized, useStatus } from '@/api/status'

/** Replaces the page with a token hint when the API answers 401. */
export function AuthGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const { error } = useStatus()
  if (!isUnauthorized(error)) return children

  return (
    <div role="alert" className="mx-auto mt-16 flex max-w-md flex-col gap-3 rounded-md border border-border bg-card p-6">
      <KeyRound className="size-5 text-brand" />
      <h2 className="text-lg font-semibold">{t('auth.title')}</h2>
      <p className="text-muted-foreground">{t('auth.body')}</p>
      <code className="rounded-sm bg-muted px-2 py-1 text-sm">{t('auth.example')}</code>
      <p className="text-sm text-muted-foreground">{t('auth.hint')}</p>
    </div>
  )
}
