<script setup lang="ts">
import axios from 'axios'
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { selectConversation } from '../desktopState'
import SubtaskTree from './SubtaskTree.vue'
type Item = {item_id:string; kind:string; title:string; status:string; outcome:string; error:string; conversation_id:string|null; conversation_title:string|null; project_id:string|null; target_run_id:string; target_kind:string; record_id:string|null; updated_at:string; event_key:string; unread:boolean; approval_count:number; pause_kind:string|null; child_history_available:boolean; research_children_available?:boolean}
const emit = defineEmits<{background:[id:string]}>(), router = useRouter()
const items = ref<Item[]>([]), view = ref('active'), filter = ref(''), cursor = ref<string|null>(null), total = ref(0), busy = ref(false), error = ref(''), muted = ref(false)
const browsingOlder = ref(false)
const childRun = ref<Item | null>(null)
const statuses:Record<string,string> = {running:'进行中', queued:'排队中', waiting_permission:'等待审批', waiting_input:'等待补充资料', waiting_budget:'等待调整预算', blocked:'受阻', partial:'部分完成', failed:'失败', interrupted:'已中断', paused:'已暂停', completed:'已完成', ended:'本轮结束，目标待核对', cancelled:'已停止'}
const kinds:Record<string,string> = {foreground:'会话',background:'后台',subtask:'子任务',input:'下一轮消息',run:'运行',framework:'论文框架',translation:'学术翻译',quality:'引用讨论分析',matrix:'证据矩阵'}
let generation = 0, timer:ReturnType<typeof setTimeout>|undefined, disposed = false, request:AbortController|null = null
function schedule() { if(timer) clearTimeout(timer); if(!disposed && !browsingOlder.value) timer = setTimeout(() => { if(document.hidden) schedule(); else void load() }, 5000) }
async function load(more=false) {
  if(more && (!cursor.value || busy.value)) return
  browsingOlder.value = more
  request?.abort(); request = new AbortController(); const token = ++generation
  busy.value = true
  try { const r = await axios.get('/api/run-center', {params:{view:view.value,status:filter.value||undefined,limit:40,cursor:more?cursor.value:undefined},signal:request.signal}); if(token !== generation || disposed) return; items.value = more ? [...new Map([...items.value,...r.data.items].map(v => [v.item_id,v])).values()] : r.data.items; cursor.value=r.data.next_cursor; total.value=r.data.total; muted.value=r.data.preferences.muted; error.value='' }
  catch(e:any) { if(token===generation && !disposed && !axios.isCancel(e)) error.value=e?.response?.data?.detail || '运行状态读取失败，保留上次结果' }
  finally { if(token===generation) { busy.value=false; schedule() } }
}
async function markRead(values=items.value.filter(v=>v.unread)) { if(!values.length) return; try { await axios.post('/api/run-center/read',{events:values.map(v=>({item_id:v.item_id,event_key:v.event_key}))}); await load() } catch { error.value='已读状态未保存，请重试' } }
async function mute() { try { const r=await axios.put('/api/run-center/preferences',{muted:!muted.value}); muted.value=r.data.muted; await load() } catch { error.value='提醒偏好未保存，请重试' } }
async function open(item:Item) {
  await markRead([item])
  if(item.target_kind==='framework' && item.conversation_id) { selectConversation(item.conversation_id); await router.push({path:'/framework',query:{run:item.target_run_id}}); return }
  if(item.target_kind==='translation' && item.conversation_id) { selectConversation(item.conversation_id); await router.push({path:'/translate',query:{run:item.target_run_id,conversation:item.conversation_id}}); return }
  if(item.target_kind==='quality' && item.conversation_id) { selectConversation(item.conversation_id); await router.push({path:'/quality',query:{run:item.target_run_id,conversation:item.conversation_id}}); return }
  if(item.target_kind==='matrix' && item.conversation_id) { selectConversation(item.conversation_id); await router.push({path:'/compare',query:{run:item.target_run_id,conversation:item.conversation_id}}); return }
  if(item.target_kind==='background' && !item.record_id) { emit('background',item.target_run_id); return }
  if(item.conversation_id) await router.push({path:'/chat',query:{conv:item.conversation_id,run:item.target_run_id,record:item.record_id||undefined}})
}
watch([view,filter],()=>{cursor.value=null;void load()})
onMounted(()=>{void load()})
onBeforeUnmount(()=>{disposed=true;generation++;request?.abort();if(timer) clearTimeout(timer)})
</script>
<template>
  <section class="run-center" aria-label="统一运行与待办">
    <div class="center-head"><div><h2>运行与待办</h2><p>会话、后台任务、持久子任务与排队消息 · {{ total }} 项</p></div><div class="center-actions"><button @click="mute">{{ muted?'恢复待办提醒':'静音待办提醒' }}</button><button @click="markRead()" :disabled="!items.some(v=>v.unread)">本页标为已读</button><button @click="load()" :disabled="busy">刷新</button></div></div>
    <div class="center-filters"><label>视图<select v-model="view"><option value="active">进行中与待处理</option><option value="attention">需要处理</option><option value="history">已结束</option><option value="all">全部</option></select></label><label>状态<select v-model="filter"><option value="">全部状态</option><option v-for="(label,key) in statuses" :key="key" :value="key">{{ label }}</option></select></label><span v-if="muted">待办提醒已静音，状态仍会更新。</span></div>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="!items.length && !busy && !error">当前没有符合条件的运行。</p>
    <ul><li v-for="item in items" :key="item.item_id" :data-run-center-id="item.item_id"><div class="item-main"><span v-if="item.unread" class="unread" aria-label="新的待处理状态"></span><span class="kind">{{ kinds[item.kind] }}</span><strong>{{ item.title }}</strong><span class="state">{{ statuses[item.status] || item.status }}</span></div><p>{{ item.conversation_title || '独立后台任务' }}<span v-if="item.approval_count"> · {{ item.approval_count }} 项待审批</span><span v-if="item.pause_kind==='context'"> · 上下文需要处理</span><span v-if="item.pause_kind==='skill_changed'"> · 技能版本已变化</span></p><p v-if="item.error" class="item-error">{{ item.error }}</p><div class="item-footer"><button v-if="item.research_children_available || item.child_history_available || ['foreground','background'].includes(item.target_kind)" :aria-expanded="childRun?.target_run_id===item.target_run_id" @click="childRun = childRun?.target_run_id===item.target_run_id ? null : item">{{ item.research_children_available ? '科研子任务与恢复' : item.child_history_available ? '子任务与尝试' : '操作回执与核对' }}</button><time>{{ new Date(item.updated_at + (/[Z+]\d*$/.test(item.updated_at)?'':'Z')).toLocaleString() }}</time><button v-if="item.conversation_id || item.target_kind==='background'" @click="open(item)">{{ item.kind==='subtask' || item.kind==='input' ? '查看所属任务' : '查看并处理' }}</button></div></li></ul>
    <SubtaskTree v-if="childRun" :run-id="childRun.target_run_id" @parent="open(childRun)" @changed="load()" />
    <button v-if="cursor" :disabled="busy" @click="load(true)">加载更多</button><p v-if="browsingOlder">查看较早记录时暂停自动刷新；点击“刷新”返回最新状态。</p><p class="center-note">状态来自实际运行记录；本轮结束不代表目标完成。审批、补充资料和恢复在对应会话或任务中处理。</p>
  </section>
</template>
<style scoped>
.run-center { padding:20px; margin-bottom:24px; border:1px solid var(--fn-border-primary); border-radius:12px; background:var(--fn-bg-primary); color:var(--fn-text-primary); }.center-head,.center-actions,.center-filters,.item-main,.item-footer { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }.center-head,.item-footer { justify-content:space-between; } h2 { margin:0;font-size:20px; } p { font-size:12px; color:var(--fn-text-secondary); line-height:1.65; } button,select { padding:7px 10px; border:1px solid var(--fn-border-primary); border-radius:7px; background:var(--fn-bg-primary); color:var(--fn-text-primary); font:inherit; font-size:12px; }button { cursor:pointer; }button:disabled { opacity:.5;cursor:default; }button:focus-visible,select:focus-visible { outline:2px solid var(--fn-brand);outline-offset:2px; }.center-filters { margin:14px 0;font-size:12px; } label { display:flex;align-items:center;gap:8px; }ul { padding:0;list-style:none;margin:0; }li { padding:15px 0;border-bottom:1px solid var(--fn-border-primary); }strong { min-width:0;overflow-wrap:anywhere;flex:1; }.kind,.state { font-size:12px;color:var(--fn-text-secondary); }.state { padding:4px 8px;background:var(--fn-bg-secondary);border-radius:6px; }.unread { width:7px;height:7px;background:var(--fn-brand);border-radius:50%; }.item-error { overflow-wrap:anywhere; }.item-footer time { font-size:11px;color:var(--fn-text-tertiary); }.center-note { margin-bottom:0; }@media(max-width:720px) { .run-center { padding:14px; }.center-actions { gap:6px; }.item-main strong { flex-basis:70%; } }
</style>
