<script setup lang="ts">
import { onMounted, ref } from 'vue'
import axios from 'axios'
import {
  getStorageStatus,
  saveStoragePolicy,
  getCleanupReceipts,
  type StorageStatus,
  type CleanupPreview,
  type CleanupReceipt,
  type StoragePolicyValues,
} from '../storageApi'
const state = ref<StorageStatus | null>(null),
  form = ref<StoragePolicyValues | null>(null),
  limitMiB = ref(10240)
const receipts = ref<CleanupReceipt[]>([]),
  nextCursor = ref<string | null>(null)
const preview = ref<CleanupPreview | null>(null),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const sizes = (bytes: number) =>
  bytes >= 1024 ** 3
    ? `${(bytes / 1024 ** 3).toFixed(2)} GB`
    : bytes >= 1024 ** 2
      ? `${(bytes / 1024 ** 2).toFixed(2)} MB`
      : `${bytes} B`
function message(e: unknown) {
  return axios.isAxiosError(e) && typeof e.response?.data?.detail === 'string'
    ? e.response.data.detail
    : '操作未完成，请重新载入后核对。'
}
async function load() {
  busy.value = true
  error.value = ''
  try {
    const value = await getStorageStatus()
    state.value = value
    form.value = { ...value.policy.values }
    limitMiB.value = value.policy.values.capacity_warning_bytes / 1024 ** 2
    const rows = await getCleanupReceipts()
    receipts.value = rows.items
    nextCursor.value = rows.next_cursor
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
async function save() {
  if (!state.value || !form.value) return
  busy.value = true
  error.value = ''
  try {
    const value = await saveStoragePolicy(state.value.policy.revision, {
      ...form.value,
      capacity_warning_bytes: Math.round(limitMiB.value * 1024 ** 2),
    })
    state.value.policy = value
    notice.value = '保留周期和容量提醒已保存。到期资料须逐项审阅后删除。'
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
async function inspect(id: string) {
  busy.value = true
  error.value = ''
  preview.value = null
  try {
    preview.value = (
      await axios.get(`/api/conversations/${encodeURIComponent(id)}/deletion-preview`)
    ).data
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
async function remove() {
  const value = preview.value
  if (!value?.can_delete || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const response = await axios.delete(
      `/api/conversations/${encodeURIComponent(value.conversation_id)}`
    )
    notice.value = `会话资料已逻辑删除，清理回执 ${response.data.cleanup?.receipt_id || '已保存'}。`
    preview.value = null
    await load()
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
async function more() {
  if (nextCursor.value === null) return
  busy.value = true
  try {
    const rows = await getCleanupReceipts(nextCursor.value)
    receipts.value.push(...rows.items)
    nextCursor.value = rows.next_cursor
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
onMounted(load)
</script>
<template>
  <section class="card storage-settings" aria-labelledby="storage-policy-title">
    <div class="card-head-row">
      <h3 id="storage-policy-title">数据保留与空间</h3>
      <button class="el-button" :disabled="busy" @click="load">刷新空间预览</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="state && form">
      <dl class="space-summary">
        <div>
          <dt>应用文件</dt>
          <dd>{{ sizes(state.storage.application_file_bytes) }}</dd>
        </div>
        <div>
          <dt>备份与恢复副本</dt>
          <dd>{{ sizes(state.storage.recovery_file_bytes) }}</dd>
        </div>
        <div>
          <dt>合计</dt>
          <dd>{{ sizes(state.storage.total_file_bytes) }}</dd>
        </div>
      </dl>
      <p>
        应用文件包含数据库内的固定原件；空间数值为当前文件大小。删除记录后，数据库文件可能保持原有大小。
      </p>
      <p v-if="state.storage.skipped_entries">
        有 {{ state.storage.skipped_entries }} 个链接或不可读取项目未计入。
      </p>
      <p v-if="state.reminders.backup_due" role="status">
        已到备份提醒周期，或尚无成功备份记录。请在上方备份卡片创建并下载备份。
      </p>
      <p v-if="state.reminders.capacity_warning" role="status">
        已达到设定的容量提醒阈值，请审阅归档资料和备份副本。
      </p>
      <form class="retention-form" @submit.prevent="save">
        <label
          >备份提醒周期（天）<input
            v-model.number="form.backup_reminder_days"
            type="number"
            min="1"
            max="3650"
            required
        /></label>
        <label
          >归档会话审阅周期（天）<input
            v-model.number="form.archived_review_days"
            type="number"
            min="1"
            max="3650"
            required
        /></label>
        <label
          >备份副本审阅周期（天）<input
            v-model.number="form.backup_review_days"
            type="number"
            min="1"
            max="3650"
            required
        /></label>
        <label
          >清理回执审阅周期（天）<input
            v-model.number="form.cleanup_receipt_review_days"
            type="number"
            min="1"
            max="3650"
            required
        /></label>
        <label
          >容量提醒阈值（MB）<input
            v-model.number="limitMiB"
            type="number"
            min="1"
            max="1073741824"
            step="1"
            required
        /></label>
        <button class="el-button" :disabled="busy" type="submit">保存保留策略</button>
      </form>
      <p>到期仅提醒审阅，资料和备份不会被自动删除。</p>
      <details>
        <summary>
          到期归档会话（{{ state.archived_review.items.length
          }}{{ state.archived_review.more ? '+' : '' }}）
        </summary>
        <ul>
          <li v-for="item in state.archived_review.items" :key="item.id">
            <RouterLink :to="{ path: '/chat', query: { conv: item.id } }">{{
              item.title
            }}</RouterLink>
            <button class="el-button" :disabled="busy" @click="inspect(item.id)">预览清理</button>
          </li>
        </ul>
        <p v-if="!state.archived_review.items.length">当前没有达到审阅周期的归档会话。</p>
      </details>
      <section v-if="preview" class="cleanup-preview" aria-label="会话清理预览">
        <h4>清理预览</h4>
        <p>
          {{ preview.total_records }} 条记录，约
          {{ sizes(preview.storage_preview.logical_payload_bytes) }}
          逻辑载荷。此数值不代表磁盘将释放的空间。
        </p>
        <p>
          删除范围包括此会话的附件、记录、笔记和成果。{{
            preview.preserved.join('；')
          }}继续保留。此操作不能撤销。
        </p>
        <ul v-if="preview.cloud_file_blockers.length">
          <li v-for="(item, i) in preview.cloud_file_blockers" :key="i">
            {{ item.state }}：{{ item.reason }}
          </li>
        </ul>
        <button class="el-button" @click="preview = null">取消</button>
        <button class="el-button" :disabled="busy || !preview.can_delete" @click="remove">
          确认删除此会话及关联资料
        </button>
      </section>
      <details>
        <summary>到期备份副本（{{ state.reminders.old_backup_count }}）</summary>
        <p>先下载并核对备份，恢复文件和原有副本继续保留。</p>
        <ul>
          <li v-for="item in state.backup_review" :key="item.id">
            {{ item.id.slice(0, 8) }} · {{ sizes(item.size_bytes) }} · {{ item.age_days }} 天
            <a :href="`/api/backups/exports/${item.id}`" download>下载检查</a>
          </li>
        </ul>
      </details>
      <details>
        <summary>清理回执（已载入 {{ receipts.length }} 条）</summary>
        <p>已有 {{ state.reminders.old_receipt_count }} 条回执达到审阅周期，列表中标为“待审阅”。</p>
        <p>
          回执保存数量、估算载荷和保留范围；不保存原文、标题、密钥或文件路径。逻辑删除不证明远端文件或历史备份已擦除。
        </p>
        <ul>
          <li v-for="item in receipts" :key="item.id">
            <details>
              <summary>
                {{ item.created_at }} · {{ item.document.total_records }} 条 ·
                {{ sizes(item.document.storage_preview.logical_payload_bytes) }}
                <span v-if="item.review_due"> · 待审阅</span>
              </summary>
              <ul>
                <li
                  v-for="group in item.document.storage_preview.categories || []"
                  :key="group.label"
                >
                  {{ group.label }}：{{ group.records }} 条，约
                  {{ sizes(group.logical_payload_bytes) }}
                </li>
              </ul>
              <p>已有备份、独立文献原件和项目文件继续保留；远端擦除状态须另行核对。</p>
              <small>回执 {{ item.id }}</small>
            </details>
          </li>
        </ul>
        <button v-if="nextCursor !== null" class="el-button" :disabled="busy" @click="more">
          更多回执
        </button>
      </details>
    </template>
  </section>
</template>
<style scoped>
.storage-settings {
  line-height: 1.7;
}
.storage-settings h3 {
  margin: 0;
}
.storage-settings p,
.storage-settings dt {
  font-size: 12px;
  color: var(--fn-text-secondary);
}
.space-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}
.space-summary dd {
  margin: 0;
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}
.retention-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 14px;
}
.retention-form label {
  display: flex;
  flex-direction: column;
  font-size: 12px;
  gap: 4px;
}
.retention-form input {
  padding: 8px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  background: var(--fn-bg-primary);
  color: inherit;
  min-width: 0;
}
.retention-form button {
  align-self: end;
}
.storage-settings summary {
  cursor: pointer;
  padding: 8px 0;
}
.storage-settings li {
  margin: 8px 0;
  overflow-wrap: anywhere;
}
.storage-settings pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11px;
}
.cleanup-preview {
  padding: 15px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  margin: 12px 0;
}
.storage-settings button:focus-visible,
.storage-settings input:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
</style>
