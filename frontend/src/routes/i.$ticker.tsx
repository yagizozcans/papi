import { createFileRoute } from '@tanstack/react-router'
import { InstrumentPage } from '@/features/instrument/instrument-page'

export const Route = createFileRoute('/i/$ticker')({
  staticData: { titleKey: 'nav.instrument' },
  component: function InstrumentRoute() {
    const { ticker } = Route.useParams()
    return <InstrumentPage ticker={ticker} />
  },
})
