import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { applyTheme, getTheme } from '@/lib/theme'
import { baseStatus, mockStatus, renderApp } from './helpers'

beforeEach(() => {
  mockStatus()
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('theme', () => {
  it('defaults to dark with empty storage, ignoring the OS preference', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
    expect(getTheme()).toBe('dark')
    applyTheme(getTheme())
    expect(document.documentElement).toHaveClass('dark')
  })

  it('toggles to light and persists the choice', async () => {
    const user = userEvent.setup()
    await renderApp()
    await user.click(await screen.findByRole('button', { name: 'Temayı değiştir' }))
    expect(localStorage.getItem('papi-theme')).toBe('light')
    expect(document.documentElement).not.toHaveClass('dark')
  })
})

describe('language', () => {
  it('switches a visible string from Turkish to English', async () => {
    const user = userEvent.setup()
    await renderApp()
    expect(await screen.findByTestId('page-title')).toHaveTextContent('Bugün')
    await user.click(screen.getByRole('button', { name: 'Dili değiştir' }))
    await waitFor(() => expect(screen.getByTestId('page-title')).toHaveTextContent('Today'))
    expect(localStorage.getItem('papi-lang')).toBe('en')
  })
})

describe('status bar', () => {
  it('renders status data', async () => {
    await renderApp()
    const lag = await screen.findByTestId('status-lag')
    expect(lag).toHaveTextContent('12s')
    expect(lag).toHaveAttribute('data-tone', 'ok')
    expect(screen.getByTestId('status-jobs-queued')).toHaveTextContent('2')
    expect(screen.getByTestId('status-version')).toHaveTextContent('papi 0.1.0')
    expect(screen.getByTestId('status-backup')).toHaveTextContent('4h')
    expect(screen.getByTestId('status-jobs-failed')).toHaveAttribute('data-tone', 'muted')
  })

  it('shows the red state when jobs failed', async () => {
    vi.unstubAllGlobals()
    mockStatus({ ...baseStatus, jobs: { queued: 0, running: 0, failed: 3 }, kap: { ...baseStatus.kap, lag_seconds: 900 } })
    await renderApp()
    const failed = await screen.findByTestId('status-jobs-failed')
    expect(failed).toHaveTextContent('3')
    expect(failed).toHaveAttribute('data-tone', 'bad')
    expect(screen.getByTestId('status-lag')).toHaveAttribute('data-tone', 'bad')
  })

  it('shows an error state when the backend is down', async () => {
    vi.unstubAllGlobals()
    mockStatus(503)
    await renderApp()
    expect(await screen.findByText('Sunucuya ulaşılamıyor', {}, { timeout: 3000 })).toBeInTheDocument()
  })

  it('shows the token screen on 401', async () => {
    vi.unstubAllGlobals()
    mockStatus(401)
    await renderApp()
    expect(await screen.findByRole('alert')).toHaveTextContent('?token=')
  })
})

describe('keyboard', () => {
  it('opens the command palette with Ctrl+K', async () => {
    const user = userEvent.setup()
    await renderApp()
    await screen.findByTestId('page-title')
    await user.keyboard('{Control>}k{/Control}')
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByPlaceholderText('Bir komut veya sayfa yaz…')).toBeInTheDocument()
    expect(within(dialog).getByText("LLM'i duraklat")).toBeInTheDocument()
  })

  it('navigates with `g p`', async () => {
    const user = userEvent.setup()
    await renderApp()
    await screen.findByTestId('page-title')
    await user.keyboard('gp')
    await waitFor(() => expect(screen.getByTestId('page-title')).toHaveTextContent('Portföy'))
  })

  it('opens the shortcuts dialog with ?', async () => {
    const user = userEvent.setup()
    await renderApp()
    await screen.findByTestId('page-title')
    await user.keyboard('?')
    expect(await screen.findByText('Klavye kısayolları')).toBeInTheDocument()
  })
})
