import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/i/$ticker')({
  staticData: { titleKey: 'nav.instrument' },
  component: () => <PagePlaceholder page="instrument" />,
})
