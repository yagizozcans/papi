import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/system')({
  staticData: { titleKey: 'nav.system' },
  component: () => <PagePlaceholder page="system" />,
})
