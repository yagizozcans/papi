import { cn } from '@/lib/utils'

export function Logo({ className }: { className?: string }) {
  return <img src={`${import.meta.env.BASE_URL}logo.svg`} alt="" className={cn('size-6', className)} />
}
