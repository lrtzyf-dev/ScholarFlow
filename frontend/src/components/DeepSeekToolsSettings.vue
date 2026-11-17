<script setup lang="ts">
import axios from 'axios'
import { onMounted, ref } from 'vue'
type Service = 'search' | 'fetch' | 'ocr' | 'research' | 'files'
interface Configuration { version: number; api_key_set: boolean; enabled_services: Service[]; endpoints: Record<string, string> }
const configuration = ref<Configuration | null>(null)
const selected = ref<Service[]>([])
const apiKey = ref('')
const busy = ref(false)
const error = ref('')
const message = ref('')
const revokeReview = ref(false)
const labels: Record<Service, string> = { search: '联网搜索', fetch: '读取网页链接', ocr: '文档与图片 OCR', research: '原生联网研究（deepseek-flash）', files: '云文件解析与读取（逐份选择上传）' }
const services: Service[] = ['search', 'fetch', 'ocr', 'research', 'files']
function apply(value: Configuration) { configuration.value = value; selected.value = [...value.enabled_services] }
function report(e: unknown) { error.value = axios.isAxiosError(e) && typeof e.response?.data?.detail === 'string' ? e.response.data.detail : '独立工具配置读取或保存失败，请检查连接后重试。' }
async function load() {
  busy.value = true; error.value = ''
  try { apply((await axios.get<Configuration>('/api/service-tools/deepseek')).data) }
  catch (e) { report(e) } finally { busy.value = false }
}
async function save() {
  if (!configuration.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const body: { expected_version: number; enabled_services: Service[]; api_key?: string } = { expected_version: configuration.value.version, enabled_services: selected.value }
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim()
    apply((await axios.put<Configuration>('/api/service-tools/deepseek', body)).data)
    apiKey.value = ''; message.value = '已保存。服务可由当前会话所选模型按需调用；保存配置没有发送收费请求。'
  } catch (e) { report(e) } finally { busy.value = false }
}
async function revoke() {
  if (!configuration.value || !revokeReview.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    apply((await axios.delete<Configuration>('/api/service-tools/deepseek/credential', { params: { expected_version: configuration.value.version } })).data)
    apiKey.value = ''; revokeReview.value = false; message.value = '独立工具密钥已撤销，相关工具已停用。聊天模型配置不受影响。'
  } catch (e) { report(e) } finally { busy.value = false }
}
onMounted(load)
</script>

<template>
  <section class="tools-card" aria-labelledby="deepseek-tools-title">
    <h3 id="deepseek-tools-title">DeepSeek 联网与文档工具</h3>
    <p>启用官方 Search、Fetch、OCR 或原生联网研究。可搭配其他聊天模型使用，服务使用这里明确配置的密钥，不自动复用聊天模型凭据。</p>
    <p class="muted">原生联网研究由 deepseek-flash 组织官方搜索和链接阅读，按模型和工具计费。研序保存实际活动与供应商引用；需要核验原文时再读取正文，引用不会冒充已核验的文献证据。</p>
    <template v-if="configuration">
      <label class="key-label" for="deepseek-tools-key">独立工具 API 密钥 <span class="muted">{{ configuration.api_key_set ? '已保存，留空保留' : '尚未配置' }}</span></label>
      <input id="deepseek-tools-key" v-model="apiKey" type="password" autocomplete="new-password" spellcheck="false" :disabled="busy" placeholder="填写服务可用的 API 密钥" />
      <fieldset :disabled="busy"><legend>允许使用的服务</legend>
        <label v-for="service in services" :key="service" class="service"><input v-model="selected" type="checkbox" :value="service" /><span>{{ labels[service] }}<small>{{ configuration.endpoints[service] }}</small></span></label>
      </fieldset>
      <p class="muted">调用可能产生服务费用，并发送查询、链接或所选原件。执行遵循会话权限与审批；断开本地连接不能保证远端取消，不会自动重试结果不明的收费调用。</p>
      <div class="actions"><button :disabled="busy" @click="save">保存独立工具配置</button><button :disabled="busy" @click="load">重新读取</button><button v-if="configuration.api_key_set" :disabled="busy" @click="revokeReview = !revokeReview">撤销工具密钥</button></div>
      <div v-if="revokeReview" class="review"><p>撤销后停用这些工具，尚未发送的旧授权会失效；已发送的远端请求可能仍会完成。</p><button :disabled="busy" @click="revoke">确认撤销</button><button :disabled="busy" @click="revokeReview = false">取消</button></div>
    </template>
    <button v-else :disabled="busy" @click="load">{{ busy ? '读取配置中…' : '重新读取配置' }}</button>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
  </section>
</template>

<style scoped>
.tools-card { padding: 22px; border: 1px solid var(--fn-border-primary); border-radius: 12px; margin-bottom: 22px; }
h3 { font-size: 16px; margin: 0 0 12px; } p { font-size: 13px; line-height: 1.75; margin: 8px 0; }.muted,small { color: var(--fn-text-secondary); font-size: 12px; }
.key-label { display: block; margin: 18px 0 8px; font-size: 13px; } .key-label span { margin-left: 8px; }
input[type=password] { box-sizing: border-box; width: 100%; max-width: 600px; border: 1px solid var(--fn-border-primary); border-radius: 7px; padding: 10px 12px; color: var(--fn-text-primary); background: var(--fn-bg-primary); }
fieldset { border: 0; padding: 0; margin: 18px 0; } legend { font-size: 13px; margin-bottom: 8px; }.service { display: flex; gap: 9px; padding: 7px 0; font-size: 13px; }.service input { align-self: flex-start; margin-top: 4px; } small { display: block; word-break: break-all; margin-top: 4px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; } button { cursor: pointer; border: 1px solid var(--fn-border-primary); border-radius: 7px; background: var(--fn-bg-primary); color: var(--fn-text-primary); padding: 8px 12px; font-size: 12px; } button:disabled { opacity: .5; cursor: default; } button:focus-visible,input:focus-visible { outline: 2px solid var(--fn-brand); outline-offset: 2px; }.error { color: #b3261e; }.review { padding: 12px; border: 1px solid var(--fn-border-primary); margin-top: 12px; }.review button { margin: 8px 8px 0 0; }
</style>
