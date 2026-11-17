<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  getBackups,
  createBackup,
  prepareRestore,
  prepareRollback,
  confirmRestore,
  cancelRestore,
  backupError,
  type BackupState,
  type BackupJob,
  type RestorePreview,
} from '../backupApi'

const state = ref<BackupState | null>(null)
const error = ref('')
const submitting = ref(false)
const preview = ref<RestorePreview | null>(null)
const agreed = ref(false)
const input = ref<HTMLInputElement | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const working = computed(
  () => submitting.value || state.value?.jobs.some((j) => ['queued', 'running'].includes(j.status))
)
const names: Record<string, string> = {
  create: '创建备份',
  prepare: '恢复预检',
  rollback: '回退预检',
  schedule: '确认恢复',
  cancel: '取消恢复',
}
const statuses: Record<string, string> = {
  queued: '等待处理',
  running: '进行中',
  succeeded: '已完成',
  failed: '未完成',
  interrupted: '已中断',
}
const sizes = (bytes = 0) =>
  bytes >= 1024 ** 3
    ? `${(bytes / 1024 ** 3).toFixed(2)} GB`
    : `${(bytes / 1024 ** 2).toFixed(2)} MB`
function credentialNotice(portability?: string, rollback = false) {
  if (portability === 'credentials_excluded_reconfigure')
    return '已排除应用凭证存储中的模型、Embedding、独立工具及 MCP 认证信息；恢复后需要重新配置。原始资料、会话和非凭证设置保留。'
  if (portability === 'protected_credentials_require_original_os_user')
    return rollback
      ? '此本机回退副本保留恢复前的受保护凭证，仅原 Windows 用户可解密使用；换机或换用户后需要重新填写。'
      : '此备份保留原 Windows 用户绑定的受保护凭证；请核对备份来源，换机或换用户后需要重新填写。'
  return '该记录未声明凭据范围，请核对备份来源与凭据保护方式；恢复后检查并重新配置所需服务。'
}
async function refresh() {
  try {
    state.value = await getBackups()
    if (working.value && !disposed) timer = setTimeout(refresh, 1000)
  } catch (e) {
    error.value = backupError(e)
  }
}
async function act(operation: () => Promise<BackupJob>, showPreview = false) {
  error.value = ''
  submitting.value = true
  clearTimeout(timer)
  try {
    const job = await operation()
    // Jobs run independently of this view; reload safely after navigation.
    await refresh()
    if (showPreview) {
      preview.value = null
      agreed.value = false
      await observePreview(job.id)
    }
  } catch (e) {
    error.value = backupError(e)
  } finally {
    submitting.value = false
    if (!disposed) {
      clearTimeout(timer)
      await refresh()
    }
  }
}
async function observePreview(id: string) {
  while (!disposed) {
    const job = state.value?.jobs.find((j) => j.id === id)
    if (job && !['queued', 'running'].includes(job.status)) {
      if (job.status === 'succeeded' && job.result?.id) preview.value = job.result as RestorePreview
      else error.value = job.error
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 700))
    if (!disposed) {
      clearTimeout(timer)
      await refresh()
    }
  }
}
function selectPreview(job: BackupJob) {
  if (job.result?.id) {
    preview.value = job.result as RestorePreview
    agreed.value = false
  }
}
async function uploaded(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (input.value) input.value.value = ''
  if (file) await act(() => prepareRestore(file), true)
}
async function confirm() {
  if (!agreed.value || !preview.value) return
  const id = preview.value.id
  await act(() => confirmRestore(id))
  if (state.value?.pending_restore) preview.value = null
}
onMounted(refresh)
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})
</script>

<template>
  <section class="backup-card" aria-labelledby="backup-title">
    <h3 id="backup-title">备份与恢复</h3>
    <p>
      保存应用数据目录内的会话、文献原件、附件固定版本、引用定位、批注、索引和非凭证设置。备份通过文件哈希和数据库完整性校验后才可下载。
    </p>
    <p v-if="state" class="scope" data-testid="backup-current-credential-policy">
      新建备份：{{ credentialNotice(state.credential_portability) }}
    </p>
    <p class="scope">
      外部项目文件夹不随备份复制，仍保留原路径。备份包含原文与会话，请妥善保存；每份历史备份和回退副本的凭据范围以其恢复预览为准。
    </p>
    <p class="scope">
      手工写在启动参数、URL、命令或资料正文中的敏感内容不会自动移除，备份仍需妥善保管。
    </p>
    <div v-if="state?.pending_restore" class="notice" role="status">
      <strong>恢复已确认，等待重启</strong>
      <p>请完全退出研序后重新打开。当前已暂停新的操作；原有数据会保留以便回退。</p>
      <button :disabled="working" @click="act(cancelRestore)">取消本次恢复</button>
    </div>
    <div class="actions">
      <button :disabled="working || !!state?.pending_restore" @click="act(createBackup)">
        创建完整备份
      </button>
      <button :disabled="working || !!state?.pending_restore" @click="input?.click()">
        选择备份并预检
      </button>
      <button
        v-if="state?.last_restore"
        :disabled="working || !!state?.pending_restore"
        @click="act(prepareRollback, true)"
      >
        准备回退到恢复前
      </button>
      <button :disabled="submitting" @click="refresh">刷新记录</button>
      <input
        ref="input"
        class="file-input"
        type="file"
        accept=".sfbak"
        aria-label="选择研序备份文件"
        @change="uploaded"
      />
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="preview && !state?.pending_restore" class="review">
      <h4>{{ preview.rollback_of ? '恢复前数据回退预览' : '恢复预览' }}</h4>
      <p>
        已校验 {{ preview.file_count }} 个文件，共
        {{ sizes(preview.total_bytes) }}。预检没有替换当前资料。
      </p>
      <p class="scope" data-testid="restore-credential-policy">
        {{ credentialNotice(preview.credential_portability, !!preview.rollback_of) }}
      </p>
      <p>
        确认后需重启生效。恢复后的订阅、排队任务、命令钩子和外部 MCP
        会暂停，权限切换为只读模式，核对后可自行重新启用。
      </p>
      <label
        ><input v-model="agreed" type="checkbox" />
        我已核对，确认用这份备份替换当前应用数据，并保留恢复前数据用于回退。</label
      >
      <div class="actions">
        <button :disabled="!agreed || working" @click="confirm">确认恢复并等待重启</button
        ><button @click="preview = null">暂不恢复</button>
      </div>
    </div>
    <ul v-if="state?.jobs.length" class="jobs" aria-label="备份恢复记录">
      <li v-for="job in state.jobs.slice(0, 8)" :key="job.id">
        <div>
          <strong>{{ names[job.kind] || job.kind }}</strong> ·
          {{ statuses[job.status] || job.status }}
          <span v-if="job.progress && job.status === 'running'"
            >（{{ job.progress.completed_files }}/{{ job.progress.total_files }} 个文件）</span
          >
          <time>{{ new Date(job.created_at).toLocaleString() }}</time>
        </div>
        <p v-if="job.error" class="error">{{ job.error }}</p>
        <p v-if="job.result && ['create', 'prepare', 'rollback'].includes(job.kind)" class="scope">
          {{ credentialNotice(job.result.credential_portability, job.kind === 'rollback') }}
        </p>
        <a
          v-if="job.status === 'succeeded' && job.result?.download_url"
          :href="job.result.download_url"
          download
          >下载已校验备份（{{ sizes(job.result.size) }}）</a
        >
        <button
          v-if="
            job.status === 'succeeded' &&
            ['prepare', 'rollback'].includes(job.kind) &&
            job.result?.id !== state.last_restore?.id &&
            !state.pending_restore
          "
          :disabled="working"
          @click="selectPreview(job)"
        >
          查看恢复预览
        </button>
      </li>
    </ul>
    <p v-else class="scope">尚无备份记录。创建备份时，请先等待运行中的任务结束并关闭项目终端。</p>
  </section>
</template>

<style scoped>
.backup-card {
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  padding: 22px;
  margin-bottom: 22px;
}
h3 {
  font-size: 16px;
  margin: 0 0 12px;
}
h4 {
  margin: 0 0 10px;
}
p {
  font-size: 13px;
  line-height: 1.75;
  margin: 8px 0;
}
.scope,
time {
  color: var(--fn-text-secondary);
  font-size: 12px;
}
.actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}
button,
a {
  font-size: 12px;
}
button {
  cursor: pointer;
  border: 1px solid var(--fn-border-primary);
  border-radius: 7px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  padding: 8px 12px;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
button:focus-visible,
a:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 3px;
}
.review,
.notice {
  margin-top: 16px;
  padding: 16px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-secondary);
}
label {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
}
input[type='checkbox'] {
  margin-top: 4px;
}
.jobs {
  list-style: none;
  padding: 0;
  margin: 18px 0 0;
}
.jobs li {
  padding: 12px 0;
  border-top: 1px solid var(--fn-border-primary);
  font-size: 12px;
}
time {
  display: block;
  margin: 4px 0;
}
.error {
  color: #b3261e;
}
a {
  color: var(--fn-brand);
}
.file-input {
  display: none;
}
</style>
