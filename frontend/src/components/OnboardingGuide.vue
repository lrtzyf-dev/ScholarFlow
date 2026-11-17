<script setup lang="ts">
import axios from 'axios'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
const props=defineProps<{conversationId:string;force?:boolean}>()
const emit=defineEmits<{configure:[];sources:[];compose:[text:string];diagnostics:[];close:[]}>()
type Journey={revision:number;status:string;show_automatically:boolean;model:{model_id:string;mode:string;configured:boolean};milestones:{id:string;title:string;complete:boolean;detail:string}[];all_observed:boolean;demo_seen:boolean}
const state=ref<Journey|null>(null),error=ref(''),busy=ref(false),demo=ref(false),demoAnswer=ref(false),demoSource=ref(false)
const visible=computed(()=>props.force || state.value?.show_automatically)
let generation=0,disposed=false
async function load(){const token=++generation;try{const r=await axios.get('/api/onboarding',{params:{conversation_id:props.conversationId}});if(!disposed&&token===generation){state.value=r.data;error.value=''}}catch{if(!disposed&&token===generation)error.value='使用引导暂时无法读取；可以继续使用会话或重新载入。'}}
async function action(action:string){if(!state.value||busy.value)return false;busy.value=true;try{const r=await axios.post('/api/onboarding/state',{action,expected_revision:state.value.revision,conversation_id:props.conversationId});state.value=r.data;error.value='';return true}catch(e:any){const failure=e?.response?.data?.detail||'引导状态未保存，请重试';await load();error.value=failure;return false}finally{busy.value=false}}
async function begin(kind:'configure'|'sources'|'compose'){if(!await action('start'))return;if(kind==='compose')emit('compose','请根据我附加的资料，概括主要发现并引用对应原文；材料没有说明的内容请明确指出。');else if(kind==='configure')emit('configure');else emit('sources')}
async function skip(){if(await action('skip'))emit('close')}
async function finish(){if(await action('complete'))emit('close')}
async function showDemo(){demo.value=true;demoAnswer.value=false;demoSource.value=false;await action('demo_seen')}
watch(()=>[props.conversationId,props.force],async()=>{await load();if(props.force&&state.value&&state.value.status!=='in_progress')await action('reopen')},{immediate:true})
function refresh(){void load()}
window.addEventListener('scholarflow:model-profiles-updated',refresh)
onBeforeUnmount(()=>{disposed=true;generation++;window.removeEventListener('scholarflow:model-profiles-updated',refresh)})
</script>
<template>
  <section v-if="visible" class="journey" aria-label="首次研究引导">
    <div class="journey-head"><div><h2>从一份资料开始</h2><p>连接模型、阅读自己的资料，再把有出处的答案保存为成果。</p></div><button :disabled="busy" @click="skip">稍后再看</button></div>
    <p v-if="error" role="alert">{{ error }}<button @click="load">重新载入引导</button></p>
    <template v-if="state">
      <p v-if="!state.model.configured" class="setup-notice">{{ state.model.mode==='mock'?'当前是主动选择的离线演示，回答不能作为真实模型能力验收。':`当前模型 ${state.model.model_id || '尚未选择'} 待配置。输入草稿和附件会保留，不会自动切换成演示。` }}</p>
      <ol><li v-for="step in state.milestones" :key="step.id"><span>{{ step.complete?'已观察到':'待完成' }}</span><div><strong>{{ step.title }}</strong><p>{{ step.detail }}</p></div></li></ol>
      <div class="journey-actions"><button :disabled="busy" @click="begin('configure')">配置或检查模型</button><button :disabled="busy" @click="begin('sources')">添加自己的资料</button><button :disabled="busy" @click="begin('compose')">准备资料问答</button><button @click="emit('diagnostics')">检查本地环境</button><button :disabled="busy" @click="showDemo">体验隔离示例</button><button v-if="state.all_observed" :disabled="busy" @click="finish">完成引导</button><button @click="load">刷新进度</button></div>
      <small>引导不要求创建项目或先入文献库。可以随时从设置重新打开；是否发送资料、入库或启用云工具由具体操作决定。</small>
    </template>
  </section>
  <ElDialog v-model="demo" title="本地交互示例 · 非模型回答" width="600px">
    <div class="journey-demo"><p>这是独立于会话和文献库的固定教学材料，不会读取你的资料、调用模型或产生接口费用。</p><blockquote>演示资料《样本记录》：在相同条件下，实验组数值为 17，对照组为 12。仅包含这一次观测，未记录误差范围或重复次数。</blockquote><button @click="demoAnswer=true">示例问题：两组有什么差别？</button><div v-if="demoAnswer"><p>固定示例答案：实验组比对照组高 5。<button @click="demoSource=true">[示例引用 1]</button>资料不足以判断统计显著性。</p><div v-if="demoSource" class="demo-source"><strong>示例原句 · 第 1 段</strong><p>实验组数值为 17，对照组为 12。</p><p>实际研究中，点击引用会打开你的固定原件版本与对应位置。</p></div></div><p>关闭示例后，使用自己的资料与真实模型继续；示例不会被当成已完成研究或真实测试。</p></div>
  </ElDialog>
</template>
<style scoped>
.journey { margin:16px auto; padding:20px; width:min(820px,calc(100% - 32px)); box-sizing:border-box; border:1px solid var(--fn-border-primary); border-radius:12px; background:var(--fn-bg-primary); color:var(--fn-text-primary); }.journey-head,.journey-actions { display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap; }.journey-actions { justify-content:flex-start; }h2 { margin:0;font-size:18px; }p,small { line-height:1.7;font-size:12px;color:var(--fn-text-secondary); }ol { list-style:none;padding:0;margin:16px 0; }li { display:flex;gap:12px;padding:9px 0;border-bottom:1px solid var(--fn-border-primary); }li>span { flex:none;font-size:11px;padding-top:2px;color:var(--fn-text-secondary); }strong { font-size:13px; }li p { margin:3px 0; }button { padding:7px 10px;cursor:pointer;font:inherit;font-size:12px;color:var(--fn-text-primary);background:var(--fn-bg-primary);border:1px solid var(--fn-border-primary);border-radius:7px; }button:disabled { opacity:.5;cursor:default; }button:focus-visible { outline:2px solid var(--fn-brand);outline-offset:2px; }small { display:block;margin-top:12px; }.setup-notice,.demo-source { padding:10px;background:var(--fn-bg-secondary);border-radius:7px; }.journey-demo { line-height:1.7; }blockquote { margin:14px 0;border-left:3px solid var(--fn-border-primary);padding-left:12px; }@media(max-width:720px){.journey{padding:14px;margin-top:10px;}}
</style>
