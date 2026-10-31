<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { RefreshCw, TerminalSquare } from 'lucide-vue-next'
import { listCommands, commandActive, commandStatus, type ProjectCommand } from '../projectCommandApi'
import { projectError } from '../projectApi'
import ProjectCommandReview from './ProjectCommandReview.vue'
const props = defineProps<{ projectId: string }>()
const commands = ref<ProjectCommand[]>([]), nextOffset = ref<number | null>(null), total = ref(0)
const error = ref(''), loading = ref(false), selected = ref<string | null>(null)
let epoch = 0, timer: ReturnType<typeof setTimeout> | undefined
async function load(offset = 0) {
  clearTimeout(timer)
  const request = ++epoch, id = props.projectId
  loading.value = true; error.value = ''
  try {
    const data = await listCommands(id, offset)
    if (request !== epoch) return
    commands.value = offset ? [...new Map([...commands.value, ...data.commands].map(c => [c.id, c])).values()] : data.commands
    nextOffset.value = data.next_offset; total.value = data.total
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally {
    if (request === epoch) {
      loading.value = false
      // Keep an expanded history stable. Explicit refresh fetches new entries.
      if (!error.value && nextOffset.value === null && commands.value.length <= 40 && commands.value.some(c => commandActive(c.status))) timer = setTimeout(() => void load(), 2500)
    }
  }
}
watch(() => props.projectId, () => { epoch++; commands.value = []; nextOffset.value = null; total.value = 0; selected.value = null; void load() }, { immediate:true })
onUnmounted(() => { epoch++; clearTimeout(timer) })
</script>

<template>
  <section class="command-history" aria-label="项目命令历史">
    <header><strong>命令记录 <span>{{ total }}</span></strong><button :disabled="loading" aria-label="刷新命令历史" @click="load()"><RefreshCw :size="14" />刷新</button></header>
    <div class="command-list" :aria-busy="loading">
      <button v-for="command in commands" :key="command.id" class="command-row" @click="selected = command.id"><TerminalSquare :size="16" /><span class="command-description"><strong>{{ command.summary }}</strong><code>{{ command.command }}</code><small>{{ new Date(command.created_at + 'Z').toLocaleString() }} · {{ command.path || '项目根目录' }}<template v-if="command.attempt > 1"> · 第 {{ command.attempt }} 次尝试</template></small></span><span class="command-status" :class="command.status">{{ commandStatus(command.status) }}<small v-if="command.exit_code !== null">退出码 {{ command.exit_code }}</small></span></button>
      <p v-if="!commands.length && !loading && !error" class="empty">暂无命令记录。在项目聊天中让助手执行构建、测试或数据处理，命令和结果会保留在这里。</p>
      <p v-if="loading" role="status" class="empty">正在读取…</p>
      <div v-if="error" role="alert" class="error">{{ error }} <button @click="load()">重试</button></div>
      <button v-if="nextOffset !== null" :disabled="loading" @click="load(nextOffset!)">加载更早命令</button>
    </div>
    <ProjectCommandReview v-model="selected" @changed="load()" />
  </section>
</template>

<style scoped>
.command-history { display:flex; flex-direction:column; flex:1; min-height:0; font-size:12px; } header { display:flex; align-items:center; justify-content:space-between; padding:8px 0; } header span { margin-left:6px; font-weight:400; color:var(--fn-text-secondary); } button { display:inline-flex; align-items:center; gap:8px; font:inherit; color:inherit; background:transparent; border:1px solid var(--fn-border); border-radius:5px; padding:7px 10px; cursor:pointer; } button:hover { background:var(--fn-bg-hover); } button:focus-visible { outline:2px solid #346ee8; outline-offset:2px; } button:disabled { opacity:.45; cursor:default; } .command-list { flex:1; min-height:0; overflow:auto; padding:2px; } .command-row { width:100%; text-align:left; margin-bottom:6px; align-items:flex-start; padding:12px 10px; } .command-description { flex:1; min-width:0; } strong,code,small { display:block; } .command-description strong { overflow-wrap:anywhere; font-weight:500; } code { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font:11px/1.7 Consolas,monospace; margin:4px 0; } small { color:var(--fn-text-secondary); font-size:11px; line-height:1.7; } .command-status { flex-shrink:0; font-size:11px; color:var(--fn-text-secondary); } .command-status.succeeded { color:#237a44; } .command-status.running { color:#346ee8; } .command-status.failed,.command-status.timed_out,.command-status.interrupted,.command-status.output_limit { color:#b42318; } .empty { color:var(--fn-text-secondary); line-height:1.8; padding:12px; } .error { color:#b42318; padding:8px; }
</style>
