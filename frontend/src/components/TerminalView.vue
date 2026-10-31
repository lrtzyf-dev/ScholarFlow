<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { terminalToken, terminalBase, type TerminalSession } from '../terminalApi'
import { projectError } from '../projectApi'
const props = defineProps<{ session: TerminalSession; visible: boolean }>()
const emit = defineEmits<{ state: [session: TerminalSession] }>()
const host = ref<HTMLElement | null>(null), error = ref(''), truncated = ref(false), connected = ref(false)
let terminal: Terminal, fit: FitAddon, socket: WebSocket | null = null, observer: ResizeObserver
let sequence = 0, epoch = 0, disposed = false, timer: ReturnType<typeof setTimeout> | undefined, retries = 0
let replayUntil = 0, pendingReply: { data: string } | null = null
function send(value: unknown) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)) }
function resize() {
  if (!host.value?.clientHeight || !host.value.clientWidth) return
  fit.fit()
  if (connected.value && props.session.status === 'running') send({ type: 'resize', cols: Math.min(300, Math.max(20, terminal.cols)), rows: Math.min(100, Math.max(5, terminal.rows)) })
}
async function connect() {
  clearTimeout(timer)
  const attempt = ++epoch
  socket?.close(); socket = null; connected.value = false; terminal.options.disableStdin = true; error.value = ''
  try {
    const token = await terminalToken(props.session.project_id, props.session.id)
    if (disposed || attempt !== epoch) return
    // Drain any already queued parser work before rebuilding from server history.
    await new Promise<void>(resolve => terminal.write('', resolve))
    if (disposed || attempt !== epoch) return
    terminal.reset(); sequence = 0; truncated.value = false; pendingReply = null
    const url = new URL(`/api${terminalBase(props.session.project_id)}/${props.session.id}/stream`, location.href)
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const channel = new WebSocket(url)
    socket = channel
    let denied = false
    channel.onopen = () => channel.send(JSON.stringify({ token, after: sequence }))
    channel.onmessage = event => {
      if (disposed || attempt !== epoch) return
      const message = JSON.parse(event.data)
      if (message.type === 'replay') {
        replayUntil = message.sequence
      } else if (message.type === 'ready') {
        connected.value = true; retries = 0; terminal.options.disableStdin = props.session.status !== 'running'
        if (props.visible) { resize(); terminal.focus() }
      } else if (message.type === 'output') {
        const reply = { data: '' }
        pendingReply = reply
        terminal.options.disableStdin = message.sequence <= replayUntil || props.session.status !== 'running'
        terminal.write(message.data, () => {
          if (disposed || attempt !== epoch) return
          pendingReply = null
          sequence = message.sequence
          send({ type: 'ack', sequence, response: reply.data })
        })
      } else if (message.type === 'reset') {
        terminal.reset(); sequence = message.sequence; truncated.value = message.truncated
      } else if (message.type === 'state') {
        terminal.options.disableStdin = !connected.value || message.session.status !== 'running'
        emit('state', message.session)
      } else if (message.type === 'error') {
        denied = true; error.value = message.message
      }
    }
    channel.onclose = () => {
      if (disposed || attempt !== epoch) return
      connected.value = false; terminal.options.disableStdin = true
      if (!error.value) error.value = '终端连接已断开'
      if (!denied && retries < 5) timer = setTimeout(() => { retries++; void connect() }, Math.min(10000, 1000 * 2 ** retries))
    }
  } catch (e) { if (attempt === epoch) error.value = projectError(e) }
}
function input(text: string) {
  if (!connected.value || props.session.status !== 'running') return
  if (pendingReply) {
    if (pendingReply.data.length + text.length > 65536) {
      error.value = '终端输入过长，请重新连接后分段粘贴'; socket?.close(); return
    }
    pendingReply.data += text
    return
  }
  const chars = Array.from(text)
  for (let i = 0; i < chars.length; i += 2048) send({ type: 'input', data: chars.slice(i, i + 2048).join('') })
}
async function copy() {
  try { if (terminal.hasSelection()) await navigator.clipboard.writeText(terminal.getSelection()) }
  catch { error.value = '复制失败' }
}
async function paste() {
  try { if (connected.value && props.session.status === 'running') terminal.paste(await navigator.clipboard.readText()) }
  catch { error.value = '剪贴板读取失败' }
}
onMounted(() => {
  terminal = new Terminal({ fontFamily: 'Consolas, "Courier New", monospace', fontSize: 12, lineHeight: 1.15,
    cursorBlink: true, scrollback: 5000, allowProposedApi: false, disableStdin: true,
    theme: { background: '#181a1c', foreground: '#e4e5e7', cursor: '#f2f3f5', selectionBackground: '#4b5774',
      black: '#1c1d20', brightBlack: '#868b92', red: '#ed6a6a', green: '#86c98b', yellow: '#e8cc83', blue: '#81acf4', magenta: '#c9a1d9', cyan: '#7dccce', white: '#d9dce1' } })
  fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(host.value!)
  terminal.textarea?.setAttribute('aria-label', '终端输入')
  terminal.attachCustomKeyEventHandler(event => {
    if (event.type === 'keydown' && event.ctrlKey && event.shiftKey && event.code === 'KeyC') { void copy(); return false }
    return true
  })
  terminal.onData(input)
  observer = new ResizeObserver(resize); observer.observe(host.value!)
  resize(); void connect()
})
watch(() => props.visible, async visible => { if (visible) { await nextTick(); resize() } })
onUnmounted(() => { disposed = true; epoch++; clearTimeout(timer); observer?.disconnect(); socket?.close(); terminal?.dispose() })
defineExpose({ interrupt: () => input('\x03'), clear: () => terminal.clear(), copy, paste, focus: () => terminal.focus() })
</script>

<template>
  <div class="terminal-view" @keydown.stop>
    <div v-if="error" class="terminal-error" role="alert"><span>{{ error }}</span><button type="button" @click="connect">重新连接</button></div>
    <div v-if="truncated" class="terminal-output-notice" role="status">较早输出已超出缓存范围</div>
    <div ref="host" class="terminal-screen" :data-connected="connected" />
  </div>
</template>
<style scoped>
.terminal-view { display:flex; flex-direction:column; flex:1; min-height:0; height:100%; background:#181a1c; }
.terminal-screen { flex:1; min-height:0; padding:6px 8px; overflow:hidden; box-sizing:border-box; }
.terminal-screen :deep(.xterm) { height:100%; }
.terminal-error,.terminal-output-notice { display:flex; align-items:center; gap:8px; flex:none; padding:4px 8px; color:#efb4af; font-size:11px; background:#292122; }
.terminal-error span { min-width:0; overflow-wrap:anywhere; }
.terminal-error button { flex:none; border:0; background:transparent; color:inherit; text-decoration:underline; font:inherit; cursor:pointer; }
.terminal-output-notice { color:#d9c68c; background:#27261e; }
</style>
