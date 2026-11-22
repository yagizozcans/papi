import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/portfolio')({
  staticData: { titleKey: 'nav.portfolio' },
  component: () => <PagePlaceholder page="portfolio" />,
})
