<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { Copy, Download, RefreshCw, Square, TerminalSquare } from 'lucide-vue-next'
import { getCommand, stopCommand, commandActive, commandStatus, type CommandDetail } from '../projectCommandApi'
import { projectError } from '../projectApi'
const id = defineModel<string | null>({ default: null })
const detail = ref<CommandDetail | null>(null), error = ref(''), actionError = ref(''), notice = ref('')
const loading = ref(false), stopping = ref(false), tail = ref(true), offset = ref(0), output = ref<HTMLElement>()
const emit = defineEmits<{ changed: [] }>()
let epoch = 0, timer: ReturnType<typeof setTimeout> | undefined
const range = computed(() => detail.value?.output.length ? `${detail.value.output_offset + 1}–${detail.value.output_offset + Array.from(detail.value.output).length} / ${detail.value.total_chars} 字符` : '暂无输出')
function clearTimer() { clearTimeout(timer); timer = undefined }
async function load() {
  if (!id.value) return
  clearTimer()
  const request = ++epoch, target = id.value
  loading.value = true; error.value = ''
  const follow = tail.value, start = offset.value
  const atBottom = !output.value || output.value.scrollHeight - output.value.scrollTop - output.value.clientHeight < 40
  try {
    const data = await getCommand(target, follow, start)
    if (request !== epoch) return
    const prior = detail.value?.status
    detail.value = data
    if (prior && prior !== data.status) emit('changed')
    await nextTick()
    if (request === epoch && follow && atBottom && output.value) output.value.scrollTop = output.value.scrollHeight
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally {
    if (request === epoch) {
      loading.value = false
      if (!error.value && detail.value && commandActive(detail.value.status)) timer = setTimeout(() => void load(), 1000)
    }
  }
}
function view(follow: boolean, start = 0) { tail.value = follow; offset.value = start; if (output.value) output.value.scrollTop = 0; void load() }
async function stop() {
  if (!id.value || stopping.value) return
  const target = id.value
  stopping.value = true; actionError.value = ''
  try { await stopCommand(target); if (id.value === target) { notice.value = '已请求停止本轮执行'; await load(); emit('changed') } }
  catch (e) { if (id.value === target) actionError.value = projectError(e) }
  finally { if (id.value === target) stopping.value = false }
}
async function copy() {
  if (!detail.value) return
  const target = id.value, text = detail.value.output
  actionError.value = ''; notice.value = ''
  try { await navigator.clipboard.writeText(text); if (id.value === target) notice.value = '已复制当前输出片段' }
  catch { if (id.value === target) actionError.value = '复制失败，可在输出区域选择文本后复制，或下载完整记录' }
}
function download() {
  if (id.value) window.open(`/api/project-commands/${encodeURIComponent(id.value)}/output`, '_blank', 'noopener')
}
watch(id, value => {
  epoch++; clearTimer(); detail.value = null; error.value = actionError.value = notice.value = ''
  tail.value = true; offset.value = 0; loading.value = stopping.value = false
  if (value) void load()
}, { immediate: true })
onUnmounted(() => { epoch++; clearTimer() })
</script>

<template>
  <ElDialog :model-value="!!id" title="命令与输出" width="960px" top="5vh" append-to-body class="command-review-dialog" :close-on-click-modal="false" @update:model-value="value => { if (!value) id = null }">
    <section class="command-review" aria-label="命令执行记录" @keydown="event => { if (event.ctrlKey || event.metaKey) event.stopPropagation() }">
      <template v-if="detail">
        <header><TerminalSquare :size="18" /><strong>{{ detail.summary }}</strong><span class="command-status" :class="detail.status">{{ commandStatus(detail.status) }}</span><button aria-label="刷新命令记录" :disabled="loading" @click="load"><RefreshCw :size="14" /></button></header>
        <div class="command-meta"><span :title="detail.cwd">{{ detail.cwd }}</span><span>PowerShell · 超时 {{ detail.timeout_seconds }} 秒</span><span v-if="detail.exit_code !== null">退出码 {{ detail.exit_code }}</span><span v-if="detail.attempt > 1">第 {{ detail.attempt }} 次尝试</span></div>
        <pre class="command-source" tabindex="0" aria-label="执行命令">{{ detail.command }}</pre>
        <p class="boundary">以当前 Windows 用户权限执行，可修改文件和访问网络。</p>
        <div class="output-toolbar"><div><button :aria-pressed="tail" @click="view(true)">跟随最新输出</button><button :aria-pressed="!tail" @click="view(false)">从头阅读</button></div><div><button :disabled="!detail.output" @click="copy"><Copy :size="13" />复制此段</button><button @click="download"><Download :size="13" />完整记录</button></div></div>
        <pre ref="output" class="command-output" tabindex="0" aria-label="命令输出">{{ detail.output || (detail.status === 'proposed' ? '等待任务卡片中的审批，尚未启动。' : detail.status === 'running' ? '命令已启动，等待输出…' : '此命令没有输出。') }}</pre>
        <footer><span>{{ range }}<template v-if="tail && commandActive(detail.status)"> · 自动刷新</template></span><div v-if="!tail"><button :disabled="loading || offset === 0" @click="view(false, Math.max(0, offset - 8000))">上一段</button><button :disabled="loading || detail.next_offset === null" @click="view(false, detail.next_offset!)">下一段</button></div></footer>
        <p v-if="detail.output_limited" class="execution-error">输出达到保存上限，记录并不完整；命令已结束。</p>
        <p v-if="detail.error" class="execution-error">{{ detail.error }}</p>
        <p v-if="detail.status === 'proposed'" class="boundary">请返回对应聊天或后台任务批准、拒绝此命令。</p>
        <p v-if="detail.status === 'interrupted'" class="boundary">检查已发生的操作后，在对应任务的恢复入口明确选择重试或跳过。</p>
        <button v-if="detail.previous_id" class="previous" @click="id = detail.previous_id">查看上次尝试</button>
      </template>
      <p v-else-if="loading" role="status">正在读取命令…</p>
      <div v-if="error" role="alert" class="execution-error">{{ error }} <button :disabled="loading" @click="load">重试读取</button></div>
      <p v-if="actionError" role="alert" class="execution-error">{{ actionError }}</p><p v-if="notice" role="status" class="boundary">{{ notice }}</p>
    </section>
    <template #footer><div class="dialog-actions"><button v-if="detail?.status === 'running'" class="stop" :disabled="stopping" @click="stop"><Square :size="13" />{{ stopping ? '正在请求…' : '停止命令与本轮执行' }}</button><button @click="id = null">关闭</button></div></template>
  </ElDialog>
</template>

<style scoped>
.command-review { color:var(--fn-text-primary); min-width:0; } header { display:flex; gap:10px; align-items:center; } header strong { flex:1; min-width:0; font-size:14px; overflow-wrap:anywhere; } .command-status { font-size:12px; white-space:nowrap; color:var(--fn-text-secondary); } .command-status.succeeded { color:#237a44; } .command-status.running { color:#346ee8; } .command-status.failed,.command-status.timed_out,.command-status.interrupted,.command-status.output_limit { color:#b42318; }
button { display:inline-flex; gap:6px; align-items:center; font:inherit; font-size:12px; color:inherit; background:transparent; border:1px solid var(--fn-border); border-radius:5px; padding:6px 10px; cursor:pointer; } button:hover { background:var(--fn-bg-hover); } button:disabled { opacity:.45; cursor:default; } button:focus-visible,pre:focus-visible { outline:2px solid #346ee8; outline-offset:2px; }
.command-meta { display:flex; flex-wrap:wrap; gap:6px 16px; font-size:11px; color:var(--fn-text-secondary); margin:10px 0; } .command-meta>span:first-child { flex-basis:100%; overflow-wrap:anywhere; } pre { white-space:pre; overflow:auto; tab-size:4; font:12px/1.7 Consolas,'Cascadia Code',monospace; margin:0; padding:12px; border:1px solid var(--fn-border); border-radius:5px; background:var(--fn-bg-main); } .command-source { max-height:100px; } .boundary { font-size:12px; line-height:1.5; color:var(--fn-text-secondary); margin:10px 0; overflow-wrap:anywhere; }
.output-toolbar,.output-toolbar>div,footer,footer>div,.dialog-actions { display:flex; align-items:center; gap:8px; } .output-toolbar,footer { justify-content:space-between; margin:10px 0; } .output-toolbar button[aria-pressed=true] { background:var(--fn-bg-secondary); font-weight:600; } .command-output { height:clamp(130px,28vh,300px); } footer { color:var(--fn-text-secondary); font-size:11px; } .execution-error { color:#b42318; font-size:12px; line-height:1.5; overflow-wrap:anywhere; } .dialog-actions { justify-content:flex-end; } .dialog-actions .stop { color:#b42318; margin-right:auto; }
</style>
