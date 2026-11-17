import axios from 'axios'

export interface QuotaGroup {
  id: string
  name: string
  origin: string
  concurrency: number
  state: 'active' | 'retired'
  active: number
  queued: number
  member_count: number
  cooldown_seconds: number
}
export interface QuotaAccount {
  id: string
  name: string
  origin: string
  config_fingerprint: string
  group_id: string | null
  same_credential_accounts: string[]
}
export interface AccountQuotas {
  revision: number
  groups: QuotaGroup[]
  accounts: QuotaAccount[]
  default_concurrency: number
  scheduler_scope: string
  policy: string
}
const base = '/api/account-quotas'
export const loadAccountQuotas = (): Promise<AccountQuotas> => axios.get(base).then((r) => r.data)
export const createQuotaGroup = (
  expected_revision: number,
  fields: Pick<QuotaGroup, 'name' | 'origin' | 'concurrency'>
): Promise<AccountQuotas> =>
  axios.post(base + '/groups', { expected_revision, ...fields }).then((r) => r.data)
export const updateQuotaGroup = (
  groupId: string,
  expected_revision: number,
  fields: Pick<QuotaGroup, 'name' | 'concurrency' | 'state'>
): Promise<AccountQuotas> =>
  axios
    .put(base + `/groups/${encodeURIComponent(groupId)}`, { expected_revision, ...fields })
    .then((r) => r.data)
export const bindQuotaAccount = (
  groupId: string,
  expected_revision: number,
  account: QuotaAccount
): Promise<AccountQuotas> =>
  axios
    .post(base + `/groups/${encodeURIComponent(groupId)}/bind`, {
      expected_revision,
      account_id: account.id,
      config_fingerprint: account.config_fingerprint,
    })
    .then((r) => r.data)
