import { createFileRoute } from '@tanstack/react-router'
import { ThesesPage } from '@/features/theses/theses-page'

export const Route = createFileRoute('/research/theses/')({
  staticData: { titleKey: 'nav.research' },
  component: ThesesPage,
})
