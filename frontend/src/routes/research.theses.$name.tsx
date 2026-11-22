import { createFileRoute } from '@tanstack/react-router'
import { ThesisPage } from '@/features/theses/thesis-page'

export const Route = createFileRoute('/research/theses/$name')({
  staticData: { titleKey: 'nav.research' },
  component: function ThesisRoute() {
    const { name } = Route.useParams()
    return <ThesisPage name={name} />
  },
})
