import axios from 'axios'
const http = axios.create({ baseURL: '/api/pricing' })
export interface PriceComponent { unit: string; subtract_units: string[]; rate: string; per: string }
export interface PriceVersion {
  id: string; name: string; endpoint: string; service: string; model_id: string | null; profile_id: string | null
  currency: string; components: PriceComponent[]; informational_units: string[]; complete_contract: boolean | number
  source_reference: string; active: boolean | number
}
export interface PriceAssessment {
  status: string; amount: string | null; currency: string | null; revision: number; tariff_id?: string; tariff_name?: string
  unknown_units?: string[]; details?: { missing_units?: string[]; unpriced_units?: string[]; reference?: string; usage_source?: string; components?: { unit: string; quantity: string; amount: string }[] }
}
export interface PricedCall {
  id: string; run_id: string; service: string; endpoint: string; model_id: string | null; state: string; usage_source: string
  actual_usage: Record<string, number>; estimated_usage: Record<string, number>; pricing: PriceAssessment
  created_at: string; provider_request_id: string | null; tariff?: PriceVersion | null
  confirmations?: { revision: number; amount: string; currency: string; reference: string; created_at: string }[]
}
export const listPriceVersions = (): Promise<{ items: PriceVersion[]; units: string[] }> => http.get('/versions').then(r => r.data)
export const createPriceVersion = (value: Omit<PriceVersion, 'id' | 'active'>): Promise<PriceVersion> => http.post('/versions', {
  ...value, complete_contract: Boolean(value.complete_contract), components: value.components.map(c => ({ ...c, rate: String(c.rate), per: String(c.per) }))
}).then(r => r.data)
export const disablePriceVersion = (id: string): Promise<PriceVersion> => http.post('/versions/' + encodeURIComponent(id) + '/disable').then(r => r.data)
export const listPricedCalls = (cid: string, offset = 0): Promise<{ items: PricedCall[]; has_more: boolean }> =>
  http.get('/conversations/' + encodeURIComponent(cid) + '/calls', { params: { limit: 20, offset } }).then(r => r.data)
export const getPricedCall = (cid: string, id: string): Promise<PricedCall> =>
  http.get('/conversations/' + encodeURIComponent(cid) + '/calls/' + encodeURIComponent(id)).then(r => r.data)
export const confirmCallCharge = (cid: string, id: string, value: { expected_revision: number; amount: string; currency: string; reference: string }): Promise<PriceAssessment> =>
  http.post('/conversations/' + encodeURIComponent(cid) + '/calls/' + encodeURIComponent(id) + '/confirm', { ...value, amount: String(value.amount) }).then(r => r.data)
export function pricingError(error: unknown) {
  const message = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof message === 'string' ? message : '费用信息未能读取或保存，请稍后重试'
}
export const priceStatus: Record<string, string> = { missing_price: '未配置价目', awaiting_usage: '用量待确认', complete_estimate: '按完整价目估算', partial_estimate: '仅部分费用估算', user_confirmed: '用户按账单核对', synthetic: '合成测试' }
