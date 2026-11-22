import { type QueryClient, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

/** Request body without `dry_run` (the hooks set it). */
export type TxnIn = Omit<components['schemas']['TxnIn'], 'dry_run'>
export type PositionSnap = components['schemas']['PositionSnap']
export type TxnResult = components['schemas']['TxnResultOut']
export type TxnType = TxnIn['type']
export type UndoOut = components['schemas']['UndoOut']
export type PendingAction = components['schemas']['PendingActionOut']

/** Everything a ledger change can touch: portfolio pages, Bugün, instrument pages, status bar. */
export function invalidatePortfolioData(qc: QueryClient) {
  return Promise.all(['portfolio', 'today', 'instrument', 'status'].map((k) => qc.invalidateQueries({ queryKey: [k] })))
}

/** Dry run: the resulting position change, nothing is written. */
export function useTradePreview() {
  return useMutation({
    mutationFn: (body: TxnIn): Promise<TxnResult> => unwrap(api.POST('/v1/portfolio/transactions', { body: { ...body, dry_run: true } })),
  })
}

export function useRecordTrade() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: TxnIn): Promise<TxnResult> => unwrap(api.POST('/v1/portfolio/transactions', { body: { ...body, dry_run: false } })),
    onSuccess: () => invalidatePortfolioData(qc),
  })
}

export function useUndoTrade() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (txnId: string | null): Promise<UndoOut> => unwrap(api.POST('/v1/portfolio/transactions/undo', { body: { txn_id: txnId } })),
    onSuccess: () => invalidatePortfolioData(qc),
  })
}

export function useApplyPending() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, quantityOverride }: { id: string; quantityOverride?: number | null }) =>
      unwrap(api.POST('/v1/portfolio/pending/{pending_id}/apply', { params: { path: { pending_id: id } }, body: { quantity_override: quantityOverride ?? null } })),
    onSuccess: () => invalidatePortfolioData(qc),
  })
}

export function useDismissPending() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => unwrap(api.POST('/v1/portfolio/pending/{pending_id}/dismiss', { params: { path: { pending_id: id } } })),
    onSuccess: () => invalidatePortfolioData(qc),
  })
}
