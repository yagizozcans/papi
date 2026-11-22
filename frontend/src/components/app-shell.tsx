import { Outlet } from '@tanstack/react-router'
import { useCallback, useState } from 'react'
import { Toaster } from 'sonner'
import { AuthGate } from '@/components/auth-gate'
import { BottomTabs } from '@/components/bottom-tabs'
import { CommandPalette } from '@/components/command-palette'
import { ShortcutsDialog } from '@/components/shortcuts-dialog'
import { Sidebar } from '@/components/sidebar'
import { StatusBar } from '@/components/status-bar'
import { Topbar } from '@/components/topbar'
import { useHotkeys } from '@/hooks/use-hotkeys'
import { useTheme } from '@/lib/theme'

export function AppShell() {
  const theme = useTheme()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)

  const togglePalette = useCallback(() => setPaletteOpen((o) => !o), [])
  const openShortcuts = useCallback(() => setShortcutsOpen(true), [])
  useHotkeys({ togglePalette, openShortcuts })

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar onSearch={() => setPaletteOpen(true)} />
        <main className="min-h-0 flex-1 overflow-y-auto">
          <AuthGate>
            <Outlet />
          </AuthGate>
        </main>
        <StatusBar />
        <BottomTabs />
      </div>
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <ShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
      <Toaster theme={theme} position="bottom-right" offset={40} />
    </div>
  )
}
