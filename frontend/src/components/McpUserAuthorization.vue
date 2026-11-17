<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mcpAuthorization, mcpAuthorizationAction, type McpServer, type McpUserAuthorization } from '../api'
const props = defineProps<{ server: McpServer }>()
const emit = defineEmits<{ changed: [] }>()
const state = ref<McpUserAuthorization>(props.server.user_oauth || { state: 'unauthorized' })
const busy = ref(false), error = ref(''), notice = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const labels: Record<string, string> = { unauthorized: '尚未登录', authorizing: '等待登录', authorized: '已授权', revoked: '已撤销', expired: '授权已过期', reauthorize: '需要重新登录' }
const url = computed(() => {
  try { const parsed = new URL(state.value.authorization_url || ''); return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : '' } catch { return '' }
})
const host = computed(() => url.value ? new URL(url.value).host : '')
function poll() {
  clearTimeout(timer)
  if (disposed || state.value.state !== 'authorizing') return
  timer = setTimeout(async () => {
    try {
      state.value = await mcpAuthorization(props.server.name)
      if (state.value.state !== 'authorizing') { notice.value = ''; emit('changed') }
    } catch { error.value = '读取授权状态失败，可以重试查询' }
    poll()
  }, 2000)
}
watch(() => props.server.user_oauth, value => { if (value) state.value = value; poll() }, { immediate: true })
async function action(kind: string) {
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await mcpAuthorizationAction(props.server.name, kind)
    notice.value = kind === 'revoke' ? result.message || '' : ''
    state.value = await mcpAuthorization(props.server.name)
    emit('changed'); poll()
  } catch (e: any) { error.value = e?.response?.data?.detail || '授权操作失败，请重试' }
  finally { busy.value = false }
}
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <div class="authorization" aria-label="用户授权">
    <p role="status">用户登录 · {{ labels[state.state] || state.state }}{{ state.refreshing ? ' · 正在刷新令牌' : '' }}</p>
    <p v-if="state.message">{{ state.message }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="actions">
      <a v-if="url" :href="url" target="_blank" rel="noopener noreferrer">前往 {{ host }} 登录</a>
      <button v-if="state.state !== 'authorizing'" :disabled="busy || !server.enabled" @click="action('login')">{{ state.state === 'authorized' ? '重新登录' : '登录授权' }}</button>
      <button v-if="state.state === 'authorizing'" :disabled="busy" @click="action('cancel')">取消授权</button>
      <button v-if="state.state === 'authorized'" :disabled="busy" @click="action('revoke')">撤销授权</button>
    </div>
    <p v-if="state.state === 'authorized'">授权已保存；连接成功后工具可用。</p>
  </div>
</template>
<style scoped>
.authorization { margin-top: 10px; font-size: 12px; overflow-wrap: anywhere; }
.authorization p { margin: 4px 0; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
button, a { color: var(--text-primary, #244635); background: var(--bg-secondary, #f3f6f3); border: 1px solid var(--border-color, #dce4dc); border-radius: 6px; padding: 6px 9px; font: inherit; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
</style>
