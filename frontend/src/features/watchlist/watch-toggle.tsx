import { Eye, EyeOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { errorText } from '../shared/states'
import { DEFAULT_WATCHLIST, useUnwatch, useWatch } from './api'

/** "İzle / İzlemeyi bırak" for the instrument header. `lists` are the watchlists that contain the ticker. */
export function WatchToggle({ ticker, watched, lists }: { ticker: string; watched: boolean; lists: string[] }) {
  const { t } = useTranslation()
  const watch = useWatch()
  const unwatch = useUnwatch()
  const busy = watch.isPending || unwatch.isPending

  const onClick = async () => {
    try {
      if (watched) {
        for (const name of lists.length > 0 ? lists : [DEFAULT_WATCHLIST]) await unwatch.mutateAsync({ ref: ticker, watchlist: name })
        toast.success(t('watchlist.unwatched', { ticker }))
      } else {
        await watch.mutateAsync({ ref: ticker })
        toast.success(t('watchlist.watched', { ticker }))
      }
    } catch (e) {
      toast.error(errorText(e) ?? t('watchlist.failed'))
    }
  }

  return (
    <Button variant="outline" size="sm" onClick={() => void onClick()} disabled={busy} aria-pressed={watched} data-testid="watch-toggle">
      {watched ? <EyeOff /> : <Eye />}
      {watched ? t('watchlist.unwatch') : t('watchlist.watch')}
    </Button>
  )
}
