import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type AlertRule = components['schemas']['AlertRuleOut']
export type AlertRules = components['schemas']['AlertRulesOut']
export type AlertRuleIn = components['schemas']['AlertRuleIn']
export type AlertRulePatch = components['schemas']['AlertRulePatch']
export type AlertChannel = components['schemas']['AlertTestIn']['channel']

export const ALERT_CHANNELS: AlertChannel[] = ['telegram', 'macos']
export const RULES_KEY = ['alerts', 'rules'] as const

export function useAlertRules() {
  return useQuery({
    queryKey: RULES_KEY,
    queryFn: ({ signal }): Promise<AlertRules> => unwrap(api.GET('/v1/alerts/rules', { signal })),
  })
}

function useRefresh() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: RULES_KEY })
}

export function useCreateRule() {
  const refresh = useRefresh()
  return useMutation({
    mutationFn: (body: AlertRuleIn): Promise<AlertRule> => unwrap(api.POST('/v1/alerts/rules', { body })),
    onSuccess: refresh,
  })
}

export function useUpdateRule() {
  const refresh = useRefresh()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: AlertRulePatch }): Promise<AlertRule> =>
      unwrap(api.PATCH('/v1/alerts/rules/{rule_id}', { params: { path: { rule_id: id } }, body: patch })),
    onSuccess: refresh,
  })
}

export function useDeleteRule() {
  const refresh = useRefresh()
  return useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE('/v1/alerts/rules/{rule_id}', { params: { path: { rule_id: id } } })),
    onSuccess: refresh,
  })
}

export function useValidateFilter() {
  return useMutation({
    mutationFn: (filter: string) => unwrap(api.POST('/v1/alerts/rules/validate', { body: { filter } })),
  })
}

export function useTestNotification() {
  return useMutation({
    mutationFn: (channel: AlertChannel) => unwrap(api.POST('/v1/alerts/test', { body: { channel } })),
  })
}
