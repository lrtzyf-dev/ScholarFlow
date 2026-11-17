<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowUpRight, Plus, RefreshCw, ThumbsDown, ThumbsUp, Trash2 } from 'lucide-vue-next'
import {
  createSubscription,
  deleteSubscription,
  giveFeedback,
  listSubscriptions,
  refreshSubscription,
  toggleSubscription,
  type SubscriptionOut,
} from '../api'

const subscriptions = ref<SubscriptionOut[]>([])
const loading = ref(true)
const error = ref('')
const newTopic = ref('')
const newInterval = ref(24)
const refreshingId = ref<string | null>(null)
const selectedId = ref<string | null>(null)
const busy = ref(false)
const selected = computed(
  () => subscriptions.value.find((sub) => sub.id === selectedId.value) || subscriptions.value[0]
)

function frequencyLabel(hours: number) {
  return (
    ({ 6: '每 6 小时', 24: '每天', 72: '每 3 天', 168: '每周' } as Record<number, string>)[hours] ||
    `每 ${hours} 小时`
  )
}
function showError(err: any) {
  error.value = err?.response?.data?.detail || err?.message || String(err)
}

async function reload() {
  try {
    const next = await listSubscriptions()
    subscriptions.value = next
    if (!next.some((sub) => sub.id === selectedId.value)) selectedId.value = next[0]?.id || null
    error.value = ''
  } catch (err: any) {
    showError(err)
  } finally {
    loading.value = false
  }
}

onMounted(reload)

async function create() {
  const topic = newTopic.value.trim()
  if (!topic || busy.value) return
  busy.value = true
  try {
    const created = await createSubscription(topic, newInterval.value)
    selectedId.value = created.id
    newTopic.value = ''
    await reload()
  } catch (err: any) {
    showError(err)
  } finally {
    busy.value = false
  }
}

async function update(action: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true
  try {
    await action()
    await reload()
  } catch (err: any) {
    showError(err)
  } finally {
    busy.value = false
  }
}
async function toggle(id: string) {
  await update(() => toggleSubscription(id))
}
async function remove(id: string) {
  await update(() => deleteSubscription(id))
}

async function refreshNow(id: string) {
  if (refreshingId.value || busy.value) return
  refreshingId.value = id
  try {
    await refreshSubscription(id)
    await reload()
  } catch (err: any) {
    showError(err)
  } finally {
    refreshingId.value = null
  }
}

async function feedback(recId: string, value: 'up' | 'down') {
  await update(() => giveFeedback(recId, value))
}
</script>

<template>
  <div class="page track-page">
    <div class="page-head">
      <div>
        <h1 class="page-title">追踪推荐</h1>
        <p class="page-desc">关注研究方向，整理值得继续阅读的文献。</p>
      </div>
    </div>

    <form class="create-box" @submit.prevent="create">
      <input
        v-model="newTopic"
        class="topic-input"
        aria-label="研究主题"
        placeholder="添加研究主题，例如：multimodal retrieval"
        required
      />
      <select v-model.number="newInterval" class="interval-select" aria-label="刷新频率">
        <option :value="6">每 6 小时</option>
        <option :value="24">每天</option>
        <option :value="72">每 3 天</option>
        <option :value="168">每周</option>
      </select>
      <button class="create-btn" type="submit" :disabled="busy || !newTopic.trim()">
        <Plus :size="15" />{{ busy ? '处理中…' : '添加订阅' }}
      </button>
    </form>

    <p v-if="error" class="err" role="alert">{{ error }}</p>
    <div v-if="loading" class="empty" role="status">正在加载订阅…</div>
    <div v-else class="track-layout">
      <aside class="track-subscriptions" aria-label="研究主题">
        <div class="section-label">
          订阅主题 <span>{{ subscriptions.length }}</span>
        </div>
        <button
          v-for="sub in subscriptions"
          :key="sub.id"
          class="track-topic"
          type="button"
          :class="{ selected: selected?.id === sub.id }"
          :aria-pressed="selected?.id === sub.id"
          @click="selectedId = sub.id"
        >
          <span class="topic-dot" :class="{ paused: !sub.active }"></span>
          <span
            >{{ sub.topic
            }}<small>{{ sub.active ? frequencyLabel(sub.interval_hours) : '已暂停' }}</small></span
          >
          <span class="topic-count">{{ sub.recommendations.length }}</span>
        </button>
        <p v-if="!subscriptions.length" class="sidebar-empty">还没有订阅主题</p>
      </aside>
      <section class="track-results" aria-label="推荐文献">
        <div v-if="!selected" class="empty">
          <h2>从一个研究主题开始</h2>
          <p>添加主题后，在这里查看 arXiv 推荐文献。</p>
        </div>
        <div v-else class="sub-card">
          <div class="sub-head">
            <div>
              <h2 class="sub-topic">{{ selected.topic }}</h2>
              <p class="run-info">
                {{ selected.active ? '订阅已启用' : '订阅已暂停' }}
                · {{ selected.last_run_at ? '上次更新 ' + selected.last_run_at : '尚未更新' }}
              </p>
              <p v-if="selected.last_status" class="run-info">{{ selected.last_status }}</p>
            </div>
            <div class="sub-actions">
              <button
                class="mini-btn"
                type="button"
                :disabled="!!refreshingId || busy"
                @click="refreshNow(selected.id)"
              >
                <RefreshCw :size="13" />{{ refreshingId === selected.id ? '刷新中…' : '立即刷新' }}
              </button>
              <button class="mini-btn" type="button" :disabled="busy" @click="toggle(selected.id)">
                {{ selected.active ? '暂停' : '启用' }}
              </button>
              <button
                class="icon-btn danger"
                type="button"
                :disabled="busy"
                aria-label="删除当前订阅"
                title="删除订阅"
                @click="remove(selected.id)"
              >
                <Trash2 :size="15" />
              </button>
            </div>
          </div>
          <div class="result-label">
            <span>推荐文献</span><span>{{ selected.recommendations.length }} 篇</span>
          </div>
          <div v-if="!selected.recommendations.length" class="empty rec-empty">
            <h3>尚无推荐文献</h3>
            <p>点击「立即刷新」抓取该主题的最新论文。</p>
          </div>
          <div v-else class="rec-list">
            <article v-for="rec in selected.recommendations" :key="rec.id" class="rec-item">
              <div class="rec-main">
                <div class="rec-meta">
                  arXiv<span v-if="rec.published_at"> · {{ rec.published_at }}</span>
                </div>
                <h3>
                  <a class="rec-title" :href="rec.url" target="_blank" rel="noopener noreferrer"
                    >{{ rec.title }} <ArrowUpRight :size="13"
                  /></a>
                </h3>
                <p v-if="rec.authors" class="rec-authors">{{ rec.authors }}</p>
                <div class="reasons">
                  <span v-for="(reason, i) in rec.reasons" :key="i" class="reason-chip">{{
                    reason
                  }}</span>
                  <a
                    v-if="rec.github_url"
                    class="github-link"
                    :href="rec.github_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    >开源代码 <ArrowUpRight :size="12"
                  /></a>
                </div>
              </div>
              <div class="rec-side">
                <span class="score">{{ rec.display_score.toFixed(2) }}<small>推荐分</small></span>
                <div class="feedback" role="group" aria-label="推荐反馈">
                  <button
                    class="fb-btn"
                    type="button"
                    :class="{ selected: rec.feedback === 'up' }"
                    :aria-pressed="rec.feedback === 'up'"
                    :disabled="busy"
                    aria-label="推荐相关"
                    title="推荐相关"
                    @click="feedback(rec.id, 'up')"
                  >
                    <ThumbsUp :size="15" />
                  </button>
                  <button
                    class="fb-btn"
                    type="button"
                    :class="{ selected: rec.feedback === 'down' }"
                    :aria-pressed="rec.feedback === 'down'"
                    :disabled="busy"
                    aria-label="推荐不相关"
                    title="推荐不相关"
                    @click="feedback(rec.id, 'down')"
                  >
                    <ThumbsDown :size="15" />
                  </button>
                </div>
              </div>
            </article>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.track-page {
  font-size: 14px;
  line-height: 1.6;
  width: 100%;
  max-width: 1180px;
  height: 100%;
  margin: 0 auto;
  padding: 38px 42px 28px;
  overflow-y: auto;
  background: var(--fn-bg-primary);
}
.page-head {
  margin-bottom: 29px;
}
.page-title {
  margin: 0 0 8px;
  font-size: 24px;
  line-height: 1.4;
  font-weight: 600;
  letter-spacing: 0;
}
.page-desc {
  margin: 0;
  line-height: 1.8;
  color: var(--fn-text-secondary);
  font-size: 13px;
}
.create-box {
  display: flex;
  gap: 9px;
  align-items: center;
  margin-bottom: 30px;
}
.topic-input,
.interval-select {
  min-height: 38px;
  border: 1px solid var(--fn-border-input);
  border-radius: 8px;
  padding: 9px 11px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  font: inherit;
  font-size: 14px;
}
.topic-input {
  min-width: 0;
  flex: 1;
}
.interval-select {
  width: 125px;
}
.create-btn,
.mini-btn,
.icon-btn,
.fb-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex-shrink: 0;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.create-btn {
  min-height: 36px;
  padding: 7px 13px;
  font-size: 14px;
  background: var(--fn-brand);
  border-color: var(--fn-brand);
  color: white;
}
button:disabled {
  opacity: 0.55;
  cursor: default;
}
.mini-btn {
  min-height: 30px;
  padding: 5px 10px;
  white-space: nowrap;
}
.icon-btn,
.fb-btn {
  width: 30px;
  height: 30px;
  border-color: transparent;
}
.mini-btn:hover:not(:disabled),
.icon-btn:hover:not(:disabled),
.fb-btn:hover:not(:disabled) {
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
}
.danger:hover:not(:disabled) {
  color: var(--fn-danger);
  background: var(--fn-danger-bg);
}
.track-layout {
  display: grid;
  grid-template-columns: 228px minmax(0, 1fr);
  min-height: 430px;
  border-top: 1px solid var(--fn-border-primary);
}
.track-subscriptions {
  border-right: 1px solid var(--fn-border-primary);
  padding: 23px 17px 10px 0;
}
.section-label {
  display: flex;
  justify-content: space-between;
  margin: 0 10px 12px;
  font-size: 11px;
  color: var(--fn-text-secondary);
}
.track-topic {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  width: 100%;
  margin-bottom: 4px;
  padding: 12px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  text-align: left;
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 12px;
  line-height: 1.55;
  cursor: pointer;
}
.track-topic:hover {
  background: var(--fn-bg-secondary);
}
.track-topic.selected {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.topic-dot {
  width: 6px;
  height: 6px;
  margin-top: 6px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--fn-brand);
}
.topic-dot.paused {
  background: var(--fn-border-strong);
}
.track-topic > span:nth-child(2) {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.track-topic small {
  display: block;
  margin-top: 5px;
  font-size: 10px;
  color: var(--fn-text-secondary);
}
.topic-count {
  margin-top: 1px;
  font-size: 11px;
  color: var(--fn-text-secondary);
}
.sidebar-empty {
  padding: 0 10px;
  font-size: 12px;
  color: var(--fn-text-secondary);
}
.track-results {
  min-width: 0;
  padding: 25px 0 0 30px;
}
.sub-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 15px;
  padding-bottom: 24px;
}
.sub-head > div:first-child {
  min-width: 0;
}
.sub-topic {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 500;
  letter-spacing: -0.4px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.run-info {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--fn-text-secondary);
  overflow-wrap: anywhere;
}
.sub-actions {
  display: flex;
  align-items: center;
  gap: 7px;
}
.result-label {
  display: flex;
  justify-content: space-between;
  border-top: 1px solid var(--fn-border-primary);
  padding: 17px 0 7px;
  font-size: 11px;
  color: var(--fn-text-secondary);
}
.rec-item {
  display: flex;
  gap: 25px;
  padding: 22px 0;
  border-bottom: 1px solid var(--fn-border-primary);
}
.rec-main {
  min-width: 0;
  flex: 1;
}
.rec-meta {
  margin-bottom: 9px;
  font-size: 10px;
  color: var(--fn-text-secondary);
}
.rec-item h3 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.65;
}
.rec-title {
  color: var(--fn-text-primary);
  overflow-wrap: anywhere;
  text-decoration: none;
}
.rec-title:hover {
  color: var(--fn-brand);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.rec-title svg,
.github-link svg {
  vertical-align: -2px;
}
.rec-authors {
  margin: 0 0 13px;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.reason-chip,
.github-link {
  padding: 2px 7px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
  color: var(--fn-text-secondary);
  font-size: 10px;
}
.github-link {
  color: var(--fn-brand);
}
.rec-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 18px;
  padding-top: 16px;
}
.score {
  color: var(--fn-text-secondary);
  font: 19px/1.1 var(--font-mono, monospace);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.score small {
  display: block;
  margin-top: 7px;
  font: 10px/1.5 var(--font-sans, sans-serif);
}
.feedback {
  display: flex;
  gap: 3px;
}
.fb-btn.selected {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.empty {
  padding: 66px 24px;
  text-align: center;
  color: var(--fn-text-secondary);
  font-size: 13px;
}
.empty h2,
.empty h3 {
  margin: 0 0 8px;
  color: var(--fn-text-primary);
  font-size: 15px;
  font-weight: 500;
}
.empty p {
  margin: 0;
}
.err {
  margin: 12px 0;
  padding: 10px 12px;
  background: var(--fn-danger-bg);
  border: 1px solid var(--fn-danger-border);
  border-radius: 8px;
  color: var(--fn-danger);
  font-size: 12px;
}
@media (max-width: 1100px) {
  .track-page {
    padding: 30px 28px;
  }
  .track-layout {
    grid-template-columns: 190px minmax(0, 1fr);
  }
  .track-results {
    padding-left: 24px;
  }
  .sub-head {
    flex-direction: column;
  }
  .sub-actions {
    align-self: flex-end;
  }
}
@media (max-width: 760px) {
  .track-page {
    padding: 25px 20px;
  }
  .create-box {
    flex-wrap: wrap;
  }
  .topic-input {
    flex-basis: 100%;
  }
  .interval-select {
    flex: 1;
  }
  .track-layout {
    display: block;
  }
  .track-subscriptions {
    display: flex;
    gap: 7px;
    overflow-x: auto;
    padding: 14px 0;
    border-right: 0;
    border-bottom: 1px solid var(--fn-border-primary);
  }
  .section-label {
    display: none;
  }
  .track-topic {
    min-width: 180px;
    max-width: 220px;
    margin: 0;
  }
  .track-results {
    padding: 22px 0 0;
  }
  .rec-item {
    gap: 15px;
  }
}
</style>
