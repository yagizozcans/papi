import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/research/agents')({
  staticData: { titleKey: 'nav.research' },
  component: () => <PagePlaceholder page="agents" />,
})
