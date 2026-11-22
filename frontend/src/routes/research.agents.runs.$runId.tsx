import { createFileRoute } from '@tanstack/react-router'
import { RunPage } from '@/features/agents/run-page'

export const Route = createFileRoute('/research/agents/runs/$runId')({
  staticData: { titleKey: 'nav.research' },
  component: function RunRoute() {
    const { runId } = Route.useParams()
    return <RunPage runId={runId} />
  },
})
