import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/lab')({
  staticData: { titleKey: 'nav.lab' },
  component: () => <PagePlaceholder page="lab" />,
})
