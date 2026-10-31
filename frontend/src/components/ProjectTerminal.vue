<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { Plus, X, Minus, Square, Copy, ClipboardPaste, Eraser, CircleSlash, RefreshCw, Maximize2 } from 'lucide-vue-next'
import { createTerminal, listTerminals, removeTerminal, stopTerminal, type TerminalSession, type TerminalRequest } from '../terminalApi'
import { projectError, type Project } from '../projectApi'
import TerminalView from './TerminalView.vue'
const props = defineProps<{ project: Project; visible: boolean }>()
const emit = defineEmits<{ hide: [] }>()
const sessions = ref<TerminalSession[]>([]), selected = ref(''), shell = ref<'powershell' | 'cmd'>('powershell')
const error = ref(''), loading = ref(false), creating = ref(false), busy = ref(''), height = ref(270)
const pending = ref<TerminalRequest | null>(null), views = new Map<string, InstanceType<typeof TerminalView>>()
const current = computed(() => sessions.value.find(s => s.id === selected.value))
let epoch = 0, observer: ResizeObserver | undefined
const root = ref<HTMLElement | null>(null), maxHeight = ref(500)
function label(s: TerminalSession) { return s.shell === 'powershell' ? 'PowerShell' : '命令提示符' }
function stateLabel(s: TerminalSession) { return s.status === 'running' ? '运行中' : s.status === 'stopped' ? '已停止' : `已退出${s.exit_code === null ? '' : ` (${s.exit_code})`}` }
async function load() {
  const request = ++epoch; loading.value = true; error.value = ''
  try {
    const items = await listTerminals(props.project.id)
    if (request !== epoch) return
    sessions.value = items
    if (!items.some(s => s.id === selected.value)) selected.value = items[0]?.id || ''
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
async function create() {
  if (creating.value || loading.value) return
  creating.value = true; error.value = ''
  pending.value ||= { request_id: crypto.randomUUID(), shell: shell.value, path: '', cols: 100, rows: 18 }
  try {
    const item = await createTerminal(props.project.id, pending.value)
    if (!sessions.value.some(s => s.id === item.id)) sessions.value.push(item)
    selected.value = item.id; pending.value = null
  } catch (e) { error.value = projectError(e) }
  finally { creating.value = false }
}
async function close(id: string) {
  if (busy.value) return
  busy.value = id; error.value = ''
  try {
    await removeTerminal(props.project.id, id)
    sessions.value = sessions.value.filter(s => s.id !== id)
    if (selected.value === id) selected.value = sessions.value.at(-1)?.id || ''
  } catch (e) { error.value = projectError(e) }
  finally { busy.value = '' }
}
async function stop() {
  if (!current.value || busy.value) return
  const id = current.value.id; busy.value = id; error.value = ''
  try { await stopTerminal(props.project.id, id) }
  catch (e) { error.value = projectError(e) }
  finally { busy.value = '' }
}
function update(item: TerminalSession) { const index = sessions.value.findIndex(s => s.id === item.id); if (index >= 0) sessions.value[index] = item }
async function tabKey(event: KeyboardEvent, index: number) {
  const count = sessions.value.length
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? count - 1
    : event.key === 'ArrowRight' ? (index + 1) % count : event.key === 'ArrowLeft' ? (index + count - 1) % count : -1
  if (next < 0) return
  event.preventDefault(); selected.value = sessions.value[next]!.id
  await nextTick()
  root.value?.querySelector<HTMLButtonElement>('[role=tab][aria-selected=true]')?.focus()
}
function resize(delta: number) { height.value = Math.max(170, Math.min(maxHeight.value, height.value + delta)) }
function drag(event: PointerEvent) {
  const el = event.currentTarget as HTMLElement, start = event.clientY, initial = height.value
  el.setPointerCapture(event.pointerId)
  const move = (e: PointerEvent) => { height.value = Math.max(170, Math.min(maxHeight.value, initial + start - e.clientY)) }
  const stop = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', stop); el.removeEventListener('pointercancel', stop) }
  el.addEventListener('pointermove', move); el.addEventListener('pointerup', stop); el.addEventListener('pointercancel', stop)
}
onMounted(() => {
  observer = new ResizeObserver(() => { maxHeight.value = Math.max(170, Math.floor((root.value?.parentElement?.clientHeight || 700) * .65)); resize(0) })
  if (root.value?.parentElement) observer.observe(root.value.parentElement)
  void load()
})
watch(() => props.visible, visible => { if (visible && !sessions.value.length) void load() })
onUnmounted(() => { epoch++; observer?.disconnect() })
</script>
<template>
  <section v-show="visible" ref="root" class="project-terminal" :style="{ height: height + 'px' }" aria-label="项目终端" @keydown.stop>
    <div class="terminal-divider" role="separator" aria-label="终端面板高度" aria-orientation="horizontal" :aria-valuenow="height" :aria-valuemin="170" :aria-valuemax="maxHeight" tabindex="0" @pointerdown="drag" @keydown.up.prevent="resize(24)" @keydown.down.prevent="resize(-24)" />
    <header class="terminal-toolbar"><strong>终端</strong><span class="terminal-project" :title="project.root_path">{{ project.name }}</span>
      <div class="terminal-tabs" role="tablist" aria-label="终端会话"><div v-for="(item, index) in sessions" :key="item.id" class="terminal-tab"><button :id="`terminal-tab-${item.id}`" role="tab" :aria-selected="selected === item.id" :aria-controls="`terminal-body-${item.id}`" :tabindex="selected === item.id ? 0 : -1" :aria-label="`${label(item)} ${index + 1}`" :title="`${item.cwd} · ${stateLabel(item)}`" @keydown="tabKey($event, index)" @click="selected = item.id">{{ label(item) }} {{ index + 1 }}<span v-if="item.status !== 'running'" class="terminal-ended" /></button><button :aria-label="`关闭终端 ${index + 1}`" title="关闭终端" :disabled="!!busy" @click="close(item.id)"><X :size="12" /></button></div></div>
      <select v-model="shell" aria-label="终端类型" :disabled="creating || !!pending"><option value="powershell">PowerShell</option><option value="cmd">命令提示符</option></select>
      <button :aria-label="pending ? '重试创建终端' : '新建终端'" :title="pending ? '重试创建终端' : '新建终端'" :disabled="creating || loading || sessions.length >= 4" @click="create"><RefreshCw v-if="pending" :size="15" /><Plus v-else :size="15" /></button>
      <button aria-label="刷新终端列表" title="刷新终端列表" :disabled="loading" @click="load"><RefreshCw :size="14" /></button>
      <button aria-label="调整终端高度" title="调整终端高度" @click="height = height >= maxHeight ? 270 : maxHeight"><Maximize2 :size="14" /></button>
      <button aria-label="收起终端" title="收起终端" @click="emit('hide')"><Minus :size="15" /></button>
    </header>
    <div v-if="error" class="terminal-panel-error" role="alert">{{ error }}</div>
    <div v-if="!sessions.length" class="terminal-empty"><span>{{ loading ? '正在读取终端…' : '没有终端会话' }}</span><button :disabled="creating || loading" @click="create"><Plus :size="14" />{{ pending ? '重试创建终端' : '新建终端' }}</button></div>
    <div v-for="item in sessions" v-show="selected === item.id" :id="`terminal-body-${item.id}`" :key="item.id" class="terminal-body" role="tabpanel" :aria-labelledby="`terminal-tab-${item.id}`"><TerminalView :ref="el => { if (el) views.set(item.id, el as InstanceType<typeof TerminalView>); else views.delete(item.id) }" :session="item" :visible="visible && selected === item.id" @state="update" /></div>
    <footer v-if="current" class="terminal-status"><span :title="current.cwd">{{ stateLabel(current) }} · PID {{ current.pid }} · {{ current.cols }}×{{ current.rows }} · {{ current.cwd }}</span><button aria-label="复制终端选区" title="复制选区" @click="views.get(selected)?.copy()"><Copy :size="13" /></button><button aria-label="粘贴到终端" title="粘贴" :disabled="current.status !== 'running'" @click="views.get(selected)?.paste()"><ClipboardPaste :size="13" /></button><button aria-label="清除终端滚动记录" title="清除滚动记录" @click="views.get(selected)?.clear()"><Eraser :size="13" /></button><button aria-label="中断终端命令" title="中断命令" :disabled="current.status !== 'running'" @click="views.get(selected)?.interrupt()"><CircleSlash :size="13" /></button><button aria-label="结束终端进程" title="结束终端进程" :disabled="!!busy || current.status !== 'running'" @click="stop"><Square :size="12" /></button></footer>
  </section>
</template>
<style scoped>
.project-terminal { display:flex; flex-direction:column; min-height:170px; flex:none; border-top:1px solid var(--fn-border,#ddd); background:var(--fn-bg-primary,#fff); color:var(--fn-text-primary,#202124); overflow:hidden; }
.terminal-divider { height:5px; flex:none; cursor:ns-resize; touch-action:none; } .terminal-divider:hover,.terminal-divider:focus-visible { background:#b3cdf7; }
button,select { font:inherit; font-size:11px; color:inherit; background:transparent; border:0; border-radius:3px; cursor:pointer; } button { display:inline-flex; align-items:center; justify-content:center; gap:5px; min-width:26px; height:26px; padding:3px 5px; flex:none; } button:hover { background:var(--fn-bg-hover,#eee); } button:disabled { opacity:.4; cursor:default; } button:focus-visible,select:focus-visible { outline:2px solid #397ce6; outline-offset:-2px; }
.terminal-toolbar { display:flex; align-items:center; gap:3px; height:34px; flex:none; padding:0 8px; border-bottom:1px solid var(--fn-border,#ddd); } .terminal-toolbar>strong { font-size:12px; font-weight:500; margin-right:6px; } .terminal-project { font-size:11px; max-width:100px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; color:var(--fn-text-secondary,#666); margin-right:7px; } .terminal-tabs { display:flex; overflow:auto; flex:1; min-width:0; gap:3px; } .terminal-tab { display:flex; align-items:center; border-bottom:2px solid transparent; flex:none; } .terminal-tab:has([aria-selected=true]) { border-color:var(--fn-brand,#2463dc); } .terminal-tab [role=tab] { white-space:nowrap; } .terminal-ended { width:5px; height:5px; border-radius:50%; background:#92979d; margin-left:2px; } select { max-width:115px; height:26px; }
.terminal-body { flex:1; min-height:0; } .terminal-empty { display:flex; align-items:center; justify-content:center; gap:12px; flex:1; font-size:12px; color:var(--fn-text-secondary,#666); } .terminal-empty button { border:1px solid var(--fn-border,#ddd); }
.terminal-status { display:flex; align-items:center; gap:3px; min-height:25px; flex:none; padding:0 7px; font-size:10px; color:var(--fn-text-secondary,#666); } .terminal-status>span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .terminal-status button { height:23px; min-width:24px; }
.terminal-panel-error { color:#ae2c25; font-size:11px; padding:4px 8px; flex:none; overflow-wrap:anywhere; }
</style>
