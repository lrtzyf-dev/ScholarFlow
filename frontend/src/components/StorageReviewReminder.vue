<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getStorageStatus, type StorageStatus } from '../storageApi'
const state = ref<StorageStatus | null>(null),
  busy = ref(false),
  error = ref('')
let epoch = 0
const reminders = computed(() => {
  const value = state.value
  if (!value) return []
  return [
    value.reminders.backup_due ? '已到备份周期或尚无成功备份记录' : '',
    value.reminders.capacity_warning ? '应用与恢复文件已达到容量提醒阈值' : '',
    value.archived_review.items.length
      ? `有 ${value.archived_review.items.length}${value.archived_review.more ? '+' : ''} 个归档会话待审阅`
      : '',
    value.reminders.old_backup_count
      ? `有 ${value.reminders.old_backup_count} 个备份副本待审阅`
      : '',
    value.reminders.old_receipt_count
      ? `有 ${value.reminders.old_receipt_count} 条清理回执待审阅`
      : '',
  ].filter(Boolean)
})
async function load() {
  const token = ++epoch
  busy.value = true
  try {
    const value = await getStorageStatus()
    if (token === epoch) {
      state.value = value
      error.value = ''
    }
  } catch {
    if (token === epoch) error.value = '数据维护状态读取失败，请刷新后核对。'
  } finally {
    if (token === epoch) busy.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  ++epoch
})
</script>
<template>
  <section class="storage-reminders" aria-label="数据维护提醒">
    <header>
      <h3>数据维护</h3>
      <button :disabled="busy" @click="load">刷新维护状态</button>
    </header>
    <p v-if="busy && !state" role="status">正在读取备份与容量状态…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul v-if="reminders.length">
      <li v-for="item in reminders" :key="item">{{ item }}</li>
    </ul>
    <p v-else-if="state">当前没有到期的数据维护提醒。</p>
    <p v-if="state">提醒依据本机记录和当前文件大小。资料与备份须逐项审阅，不会自动删除。</p>
    <RouterLink :to="{ path: '/settings', query: { tab: 'data' } }"
      >查看备份、保留策略与清理回执</RouterLink
    >
  </section>
</template>
<style scoped>
.storage-reminders {
  margin: 0 0 24px;
  padding: 18px 20px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  background: var(--fn-bg-primary);
  font-size: 12px;
  line-height: 1.7;
}
header {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
h3 {
  margin: 0;
  font-size: 16px;
}
button {
  padding: 6px 9px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  color: inherit;
  border-radius: 6px;
  cursor: pointer;
}
ul {
  padding-left: 20px;
}
p {
  color: var(--fn-text-secondary);
}
button:focus-visible,
a:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
</style>
