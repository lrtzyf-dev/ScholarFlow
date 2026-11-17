<script setup lang="ts">
import axios from 'axios'
import { ref, watch, onBeforeUnmount } from 'vue'
import type { SourceRevision } from '../sourceApi'
const props = defineProps<{ conversationId: string; sources: SourceRevision[] }>()
type Binding = { id: string; version: number; state: string; title: string; purpose: string; raw_sha256: string; size_bytes: number; remote_size_bytes?: number; remote_size_matches_original?: boolean; verification_note?: string; expires_seconds: number; expires_at: string | null; error: string; remote_file_id?: string; candidates: {id: string; filename: string; bytes: number}[]; next_cursor?: string }
const records = ref<Binding[]>([]), busy = ref(false), error = ref(''), selected = ref(''), purpose = ref('user_data'), hours = ref(24), review = ref<Binding | null>(null)
const manualReview = ref('')
let generation = 0
const states: Record<string, string> = { prepared: '待确认上传', uploading: '上传中或结果待核查', available: '可用', unknown: '上传结果未知', remote_found_unknown: '找到候选，仅可清理', expired: '已过期', delete_pending: '删除待确认', deleted: '远端已删除或未上传', manual_cleanup_acknowledged: '用户声明已清理（服务端未验证）' }
function explain(e: unknown) { return axios.isAxiosError(e) && typeof e.response?.data?.detail === 'string' ? e.response.data.detail : '云文件操作未完成，请重新读取本地状态。' }
async function load() {
  const token = ++generation
  error.value = ''; review.value = null; manualReview.value = ''
  try { const r = await axios.get('/api/provider-files', {params: {conversation_id: props.conversationId}}); if (token === generation) records.value = r.data.items }
  catch(e) { if (token === generation) error.value = explain(e) }
}
watch(() => props.conversationId, () => { records.value = []; selected.value = ''; void load() }, {immediate: true})
watch(selected, () => { const title = props.sources.find(v => v.revision_id === selected.value)?.title || ''; purpose.value = /\.(pdf|docx?|pptx?)$/i.test(title) ? 'text_recognition' : 'user_data' })
onBeforeUnmount(() => { generation++ })
async function prepare() {
  if (busy.value || !selected.value) return
  const cid = props.conversationId, token = generation
  busy.value = true; error.value = ''
  try { const r = await axios.post('/api/provider-files/prepare', {conversation_id: cid, source_revision_id: selected.value, purpose: purpose.value, expires_seconds: hours.value * 3600, request_id: crypto.randomUUID()}); if (token === generation) { review.value = r.data; records.value = [r.data, ...records.value.filter(v => v.id !== r.data.id)] } }
  catch(e) { if(token === generation) error.value = explain(e) } finally { busy.value = false }
}
async function action(row: Binding, operation: string, candidate?: string) {
  if (busy.value) return
  const cid = props.conversationId, token = generation
  busy.value = true; error.value = ''
  try { const r = await axios.post(`/api/provider-files/${row.id}/actions`, {conversation_id: cid, expected_version: row.version, action: operation, ...(candidate ? {candidate_id: candidate} : {}), ...(operation === 'acknowledge_manual_cleanup' && manualReview.value === row.id ? {confirm_manual_cleanup: true} : {}), ...(operation === 'reconcile' && row.next_cursor ? {cursor: row.next_cursor} : {})}); if (token === generation) { records.value = records.value.map(v => v.id === row.id ? r.data : v); review.value = null; manualReview.value = '' } }
  catch(e) { if(token === generation) { error.value = explain(e); const r = await axios.get('/api/provider-files', {params: {conversation_id: cid}}); if(token === generation) records.value = r.data.items } }
  finally { busy.value = false }
}
</script>

<template>
  <details class="cloud-files">
    <summary>DeepSeek 云文件 · 逐份上传、过期与清理</summary>
    <p>先在设置启用“云文件解析与读取”。只上传这里确认的固定原件，默认保留 24 小时；原件仍保存在本地。上传与解析、后续模型读取可能计费。</p>
    <div class="cloud-form">
      <label>固定原件<select aria-label="固定原件" v-model="selected" :disabled="busy"><option value="">选择文件版本</option><option v-for="source in sources" :key="source.revision_id" :value="source.revision_id">{{ source.title }} · v{{ source.version }}</option></select></label>
      <label>用途<select aria-label="用途" v-model="purpose" :disabled="busy"><option value="user_data">文本、Markdown、CSV、表格或图片</option><option value="text_recognition">文档文字解析</option><option value="rich_layout_recognition">文档图文解析</option></select></label>
      <label>有效期<select aria-label="有效期" v-model="hours" :disabled="busy"><option :value="1">1 小时</option><option :value="24">24 小时</option><option :value="168">7 天</option><option :value="720">30 天</option></select></label>
      <button :disabled="busy || !selected" @click="prepare">查看上传内容</button>
      <button :disabled="busy" @click="load">刷新本地记录</button>
    </div>
    <div v-if="review" class="cloud-review"><strong>{{ review.title }}</strong><p>{{ review.size_bytes }} 字节 · {{ review.purpose }} · 保留 {{ review.expires_seconds / 3600 }} 小时</p><small>SHA-256：{{ review.raw_sha256 }}</small><button v-if="review.state === 'prepared'" :disabled="busy" @click="action(review, 'upload')">确认发送原件到 DeepSeek</button><p v-else>已有记录：{{ states[review.state] || review.state }}，请在下方继续核查。</p></div>
    <ul><li v-for="row in records" :key="row.id"><strong>{{ row.title }}</strong> · {{ states[row.state] || row.state }}<small v-if="row.expires_at">远端到期：{{ new Date(row.expires_at).toLocaleString() }}</small><p v-if="row.error">{{ row.error }}</p>
      <div class="cloud-actions">
        <button v-if="row.state === 'prepared'" :disabled="busy" @click="review = row">审阅上传</button>
        <button v-if="row.remote_file_id && row.state !== 'deleted'" :disabled="busy" @click="action(row, 'refresh')">查询远端状态</button>
        <button v-if="['unknown','uploading'].includes(row.state)" :disabled="busy" @click="action(row, 'reconcile')">{{ row.next_cursor ? '继续核查远端列表' : '核查远端列表' }}</button>
        <button v-if="row.remote_file_id && row.state !== 'deleted'" :disabled="busy" @click="action(row, 'delete')">删除此远端文件</button>
        <button v-if="!['prepared','deleted','manual_cleanup_acknowledged'].includes(row.state)" :disabled="busy" @click="manualReview = row.id">我已在服务账户手工清理</button>
        <button v-for="candidate in row.candidates || []" :key="candidate.id" :disabled="busy" @click="action(row, 'adopt_for_cleanup', candidate.id)">关联候选以清理：{{ candidate.id }} · {{ candidate.bytes }} 字节</button>
      </div>
      <div v-if="manualReview === row.id" class="cloud-review"><p>仅在你已登录 DeepSeek 服务账户并手工删除此文件后确认。研序会记录你的声明，允许后续删除本地会话；不会显示为“服务端已验证删除”。</p><button :disabled="busy" @click="action(row, 'acknowledge_manual_cleanup')">确认已手工清理，记录未验证声明</button><button :disabled="busy" @click="manualReview = ''">取消</button></div>
      <p v-if="row.state === 'available'">在当前会话附加对应的原件后，可要求 Agent 使用云文件读取。</p>
      <p v-if="row.state === 'available' && row.remote_size_matches_original === false">服务端报告 {{ row.remote_size_bytes }} 字节，本地原件为 {{ row.size_bytes }} 字节。服务端可能已转换或解析；引用仍须返回本地原文核验。</p>
      <small v-if="row.state === 'available'">{{ row.verification_note }}</small>
    </li></ul>
    <p v-if="busy" role="status">操作进行中，请勿重复提交…</p><p v-if="error" role="alert">{{ error }}</p>
    <p>仅核查元数据不能证明丢失响应的上传成功。未知结果不会自动重传；本地删除与远端清理是两项不同操作。</p>
  </details>
</template>

<style scoped>
.cloud-files { margin: 14px 0; border: 1px solid var(--fn-border-primary); padding: 12px; border-radius: 8px; font-size: 12px; overflow-wrap: anywhere; }
summary { cursor: pointer; font-weight: 600; }.cloud-form,.cloud-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: end; } label { display: flex; flex-direction: column; gap: 5px; min-width: 0; max-width: 100%; } select { max-width: 280px; padding: 6px; color: var(--fn-text-primary); background: var(--fn-bg-primary); border: 1px solid var(--fn-border-primary); border-radius: 6px; } p { line-height: 1.7; } small { display: block; color: var(--fn-text-secondary); margin: 6px 0; }.cloud-review { margin: 12px 0; padding: 12px; border: 1px solid var(--fn-border-primary); } ul { padding-left: 18px; } li { padding: 10px 0; } button { margin-top: 5px; cursor: pointer; font: inherit; padding: 7px 10px; color: var(--fn-text-primary); background: var(--fn-bg-primary); border: 1px solid var(--fn-border-primary); border-radius: 6px; } button:disabled { opacity: .5; cursor: default; } button:focus-visible,select:focus-visible,summary:focus-visible { outline: 2px solid var(--fn-brand); outline-offset: 2px; } code { user-select: all; }
</style>
