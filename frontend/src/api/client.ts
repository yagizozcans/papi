import createClient from 'openapi-fetch'
import type { paths } from './schema'

export class ApiError extends Error {
  status: number
  constructor(status: number, message?: string) {
    super(message ?? `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
  }
}

// Same-origin: in production FastAPI serves both the UI and /v1; in dev Vite proxies /v1.
export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? '',
  credentials: 'same-origin',
  fetch: (request) => globalThis.fetch(request),
})
