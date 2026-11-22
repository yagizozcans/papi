import { useLocation } from '@tanstack/react-router'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertsPanel } from '../alerts/alerts-panel'
import { LlmPanel } from '../llm/llm-panel'
import { useDocumentTitle } from '../shared/use-document-title'

/** Sistem: panels stack here (P2: LLM, Alerts; P3 adds health, data sources, settings, backups ...). */
export function SystemPage() {
  const { t } = useTranslation()
  useDocumentTitle(t('nav.system'))
  const hash = useLocation({ select: (l) => l.hash })

  // The status bar links to /system#llm: scroll the panel into view once it is rendered.
  useEffect(() => {
    if (hash) document.getElementById(hash)?.scrollIntoView({ block: 'start' })
  }, [hash])

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-3 p-3 md:p-4">
      <LlmPanel />
      <AlertsPanel />
    </div>
  )
}
