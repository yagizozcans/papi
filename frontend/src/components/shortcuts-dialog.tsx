import { useTranslation } from 'react-i18next'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { NAV } from '@/lib/nav'

function Kbd({ children }: { children: string }) {
  return <kbd className="rounded-sm border border-border bg-muted px-1.5 py-0.5 font-mono text-xs">{children}</kbd>
}

export function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>{t('shortcuts.title')}</DialogTitle>
        <DialogDescription>{t('shortcuts.description')}</DialogDescription>
        <dl className="grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1">
          <dt>{t('shortcuts.palette')}</dt>
          <dd><Kbd>⌘K</Kbd> / <Kbd>Ctrl K</Kbd></dd>
          <dt>{t('shortcuts.help')}</dt>
          <dd><Kbd>?</Kbd></dd>
          {NAV.map(({ key, hotkey }) => (
            <div key={key} className="col-span-2 grid h-row grid-cols-subgrid items-center border-t border-border">
              <dt>{t('shortcuts.goTo')}: {t(`nav.${key}`)}</dt>
              <dd className="flex items-center gap-1"><Kbd>g</Kbd><span className="text-xs text-muted-foreground">{t('shortcuts.then')}</span><Kbd>{hotkey}</Kbd></dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  )
}
