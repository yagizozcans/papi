import { createFileRoute } from '@tanstack/react-router'
import { AgentsPage } from '@/features/agents/agents-page'

export const Route = createFileRoute('/research/agents/')({
  staticData: { titleKey: 'nav.research' },
  component: AgentsPage,
})
