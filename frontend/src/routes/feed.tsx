import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/feed')({
  staticData: { titleKey: 'nav.feed' },
  component: () => <PagePlaceholder page="feed" />,
})
