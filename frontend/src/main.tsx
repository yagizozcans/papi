import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './i18n'
import { createAppRouter } from './router'
import { applyTheme, getTheme } from './lib/theme'
import './styles/index.css'

applyTheme(getTheme())

const queryClient = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: true } } })
const router = createAppRouter()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
