import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { render } from '@testing-library/react'
import { vi } from 'vitest'
import type { Status } from '@/api/status'
import '@/i18n'
import i18n from '@/i18n'
import { createAppRouter } from '@/router'

export const baseStatus: Status = {
  version: '0.1.0',
  running: true,
  heartbeat_at: '2026-11-22T10:00:00Z',
  heartbeat_stale: false,
  kap: { last_poll_at: '2026-11-22T09:59:50Z', last_poll_ok_at: '2026-11-22T09:59:50Z', lag_seconds: 12 },
  jobs: { queued: 2, running: 1, failed: 0 },
  lanes: {},
  governor: { paused: false, until: null, reason: null },
  telegram: { enabled: true, ready: true },
  last_backup_at: '2026-11-22T06:00:00Z',
  server_time: '2026-11-22T10:00:00Z',
}

/** Stub fetch: `/v1/status` answers with `status` (or the given HTTP status). */
export function mockStatus(status: Status | number = baseStatus) {
  const fetchMock = vi.fn(async (req: Request | string) => {
    const url = typeof req === 'string' ? req : req.url
    if (url.endsWith('/v1/status')) {
      if (typeof status === 'number') return new Response('{}', { status })
      return Response.json(status)
    }
    return Response.json({ paused: false, until: null, reason: null })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

export async function renderApp(path = '/app/') {
  await i18n.changeLanguage('tr')
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { router, queryClient, ...utils }
}
