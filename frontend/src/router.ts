import { createRouter, type RouterHistory } from '@tanstack/react-router'
import { routeTree } from './routeTree.gen'

export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, basepath: '/app', history, defaultPreload: 'intent' })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
  interface StaticDataRouteOption {
    /** i18n key of the page title shown in the top bar. */
    titleKey?: string
  }
}
