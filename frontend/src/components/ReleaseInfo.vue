<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import axios from 'axios'

const frontend = __SCHOLARFLOW_RELEASE__
const backend = ref<{version:string; channel:string; build_number:number; build_id:string; build_manifest_available:boolean; publisher_signature:string; distribution_state:string} | null>(null)
const error = ref('')
const loading = ref(false)
const mismatch = computed(() => backend.value && (frontend.version !== backend.value.version || frontend.channel !== backend.value.channel || frontend.build_number !== backend.value.build_number))
const channel = (value: string) => ({development:'开发版', candidate:'候选版', stable:'稳定通道'}[value] || value)
async function refresh() {
  loading.value = true; error.value = ''
  try { backend.value = (await axios.get('/api/release')).data }
  catch { backend.value = null; error.value = '暂时无法读取后端版本，请检查本地服务后重试。' }
  finally { loading.value = false }
}
onMounted(refresh)
</script>

<template>
  <section class="release-card" aria-labelledby="release-title">
    <div class="release-head"><h2 id="release-title">版本与构建</h2><button type="button" :disabled="loading" @click="refresh">{{ loading ? '检查中…' : '重新检查版本' }}</button></div>
    <dl>
      <div><dt>界面版本</dt><dd>{{ frontend.version }} · {{ channel(frontend.channel) }} · 构建 {{ frontend.build_number }}</dd></div>
      <div v-if="backend"><dt>服务版本</dt><dd>{{ backend.version }} · {{ channel(backend.channel) }} · 构建 {{ backend.build_number }}</dd></div>
      <div v-if="backend"><dt>构建标识</dt><dd class="build-id">{{ backend.build_id === 'development' ? '源码开发环境' : backend.build_id }}</dd></div>
    </dl>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="mismatch" class="release-warning" role="alert">界面与服务版本不一致，请关闭研序后使用同一份完整程序重新启动。</p>
    <p v-else-if="backend" role="status">界面与服务版本一致。</p>
    <p v-if="backend" class="release-note">{{ backend.build_manifest_available ? '已附构建输入清单，可离线核对完整性。' : '当前没有安装构建清单。' }} {{ backend.publisher_signature === 'unsigned' ? '此构建尚未签名。' : '发布者签名尚未验证。' }} 构建清单不代表安装升级或商用验收已通过。</p>
  </section>
</template>

<style scoped>
.release-card{padding:24px 0;border-bottom:1px solid var(--fn-border-primary);color:var(--fn-text-primary);font-size:14px;line-height:1.65}
.release-head{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
h2{font-size:16px;margin:0;font-weight:600}
button{font:inherit;background:var(--fn-bg-secondary);border:1px solid var(--fn-border-primary);color:inherit;padding:6px 12px;border-radius:7px;cursor:pointer}
button:focus-visible{outline:2px solid var(--fn-accent,#5071d5);outline-offset:3px}
button:disabled{opacity:.6;cursor:wait}
dl{margin:18px 0 8px}dl>div{display:grid;grid-template-columns:100px minmax(0,1fr);gap:12px;margin:7px 0}dt{color:var(--fn-text-secondary)}dd{margin:0}.build-id{overflow-wrap:anywhere;font-family:monospace;font-size:12px}
.release-note{color:var(--fn-text-secondary);font-size:12px}.release-warning{color:var(--fn-danger,#a94428)}
@media(max-width:560px){dl>div{grid-template-columns:1fr;gap:0}}
</style>
