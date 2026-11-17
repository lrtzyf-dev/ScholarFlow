<script setup lang="ts">
import {ref,watch} from 'vue'
import axios from 'axios'
const props=defineProps<{conversationId:string;disabled?:boolean;running?:boolean}>()
const emit=defineEmits<{busy:[boolean]}>()
type Policy={revision:number;mode:'host'|'container';image:string;write_path:string}
const saved=ref<Policy>(), form=ref<Policy>({revision:0,mode:'host',image:'',write_path:''})
const busy=ref(false),checking=ref(false),error=ref(''),notice=ref('')
let epoch=0
watch(()=>props.conversationId,async cid=>{
  const token=++epoch; saved.value=undefined; checking.value=false;error.value='';notice.value='';busy.value=true;emit('busy',true)
  try{const {data}=await axios.get<Policy>(`/api/conversation-execution-environments/${encodeURIComponent(cid)}`)
    if(token===epoch){saved.value=data;form.value={...data}}}
  catch(e:any){if(token===epoch)error.value=e.response?.data?.detail||'执行环境未能加载，请重试'}
  finally{if(token===epoch){busy.value=false;emit('busy',false)}}
},{immediate:true})
async function save(){
  if(!saved.value)return
  const token=epoch;busy.value=true;emit('busy',true);error.value='';notice.value=''
  try{const {revision,...policy}=form.value
    const {data}=await axios.put<Policy>(`/api/conversation-execution-environments/${encodeURIComponent(props.conversationId)}`,{...policy,expected_revision:revision})
    if(token===epoch){saved.value=data;form.value={...data};notice.value='已保存；新的任务按此环境运行，既有命令不会静默切换。'}}
  catch(e:any){if(token===epoch)error.value=e.response?.data?.detail||'保存失败，输入已保留；请重新载入当前设置后再保存'}
  finally{if(token===epoch){busy.value=false;emit('busy',false)}}
}
async function check(){
  const token=epoch;checking.value=true;error.value=''
  try{const {data}=await axios.get('/api/execution-environments/diagnostics',{params:{image:form.value.image}})
    if(token===epoch)notice.value=data.message}
  catch(e:any){if(token===epoch)error.value=e.response?.data?.detail||'本地引擎检查未完成'}
  finally{if(token===epoch)checking.value=false}
}
</script>
<template>
  <details class="execution-control">
    <summary aria-label="会话执行环境">{{saved?.mode==='container'?'受限容器':'本机执行'}}</summary>
    <div class="environment-panel">
      <h4>命令执行环境</h4>
      <p>审批模式决定是否确认操作，执行环境决定命令能访问的范围。</p>
      <label>执行方式<select v-model="form.mode" aria-label="命令执行方式" :disabled="busy||disabled"><option value="host">本机 PowerShell（当前用户权限）</option><option value="container">受限 Linux 容器（Docker Desktop）</option></select></label>
      <template v-if="form.mode==='container'">
        <label>固定镜像摘要<input v-model.trim="form.image" aria-label="容器固定镜像" placeholder="registry/image@sha256:…" :disabled="busy||disabled" /></label>
        <label>可写输出目录<input v-model="form.write_path" aria-label="容器输出目录" placeholder="项目内已有目录，可留空" :disabled="busy||disabled" /></label>
        <p>Linux /bin/sh 命令；网络关闭，项目只读。可选目录仅映射到 /outputs 可写。容器以非 root 运行，有内存和进程限制。须先准备镜像，不会自动下载，也不会失败后改成本机执行。</p>
        <button type="button" :disabled="checking||busy" @click="check">{{checking?'正在检查…':'检查本地容器引擎'}}</button>
      </template>
      <p v-else>本机命令能以当前用户身份访问本机和网络，项目起始目录校验不构成操作系统沙箱。</p>
      <p v-if="running">当前任务的既有环境保持冻结；更改会阻止尚未开始且环境不匹配的命令，需重新审阅。</p>
      <button type="button" :disabled="busy||disabled||!saved" @click="save">保存任务执行环境</button>
      <p v-if="error" role="alert" class="error">{{error}}</p><p v-if="notice" role="status">{{notice}}</p>
    </div>
  </details>
</template>
<style scoped>
.execution-control{position:relative;font-size:12px;color:var(--fn-text-secondary)}
summary{cursor:pointer;padding:6px;white-space:nowrap}.environment-panel{position:absolute;bottom:36px;right:0;width:min(380px,80vw);max-height:70vh;overflow:auto;z-index:2600;padding:18px;background:var(--fn-bg-primary);border:1px solid var(--fn-border-primary);border-radius:8px;box-shadow:0 6px 24px #10203020}
h4{margin:0 0 8px}label{display:block;margin:12px 0}input,select{display:block;box-sizing:border-box;width:100%;padding:8px;margin-top:5px;background:var(--fn-bg-primary);color:var(--fn-text-primary);border:1px solid var(--fn-border-input);border-radius:5px}p{line-height:1.7}button{cursor:pointer;border:1px solid var(--fn-border-primary);border-radius:5px;background:var(--fn-bg-secondary);color:var(--fn-text-primary);padding:7px;margin:4px 8px 4px 0}.error{color:var(--fn-danger-text,#b42318)}:focus-visible{outline:2px solid var(--fn-brand)}
</style>
