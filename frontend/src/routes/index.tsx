import { createFileRoute } from '@tanstack/react-router'
import { TodayPage } from '@/features/today/today-page'

export const Route = createFileRoute('/')({
  staticData: { titleKey: 'nav.today' },
  component: TodayPage,
})
