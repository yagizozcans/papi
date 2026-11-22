import { Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'

/** Link to the disclosure reader (`/feed/$eventId`, owned by the Feed page). */
export function DisclosureLink({
  eventId,
  className,
  children,
}: {
  eventId: string
  className?: string
  children: ReactNode
}) {
  return (
    <Link to="/feed/$eventId" params={{ eventId }} className={className}>
      {children}
    </Link>
  )
}
