import { createFileRoute } from '@tanstack/react-router'
import { FeedPage } from '@/features/feed/feed-page'
import { validateFeedSearch } from '@/features/feed/search'

export const Route = createFileRoute('/feed')({
  staticData: { titleKey: 'nav.feed' },
  validateSearch: validateFeedSearch,
  component: FeedPage,
})
