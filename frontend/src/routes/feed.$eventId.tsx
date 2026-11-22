import { createFileRoute } from '@tanstack/react-router'

// The reader is rendered by the parent /feed route (list and reader share one split view);
// this route only owns the deep link /feed/<eventId>.
export const Route = createFileRoute('/feed/$eventId')({
  component: () => null,
})
