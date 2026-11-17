<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { activeConversationId } from '../desktopState'
import { ensureSourceConversation, sourceError } from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'
import {
  bibliography as api,
  type BibliographyBatch,
  type BibliographyEntry,
  type ZoteroBinding,
  type ZoteroItem,
} from '../bibliographyApi'
const emit = defineEmits<{ changed: [] }>()
const router = useRouter()
const cid = ref(activeConversationId.value)
const format = ref('bibtex'),
  content = ref(''),
  title = ref('书目导入'),
  busy = ref(''),
  error = ref(''),
  notice = ref('')
const fileInput = ref<HTMLInputElement | null>(null),
  batch = ref<BibliographyBatch | null>(null)
const batches = ref<{ id: string; title: string; format: string }[]>([])
let requestId = crypto.randomUUID(),
  requestSignature = ''
const bindings = ref<ZoteroBinding[]>([]),
  userId = ref(''),
  apiKey = ref(''),
  collection = ref('')
const collections = ref<{ key: string; name: string }[]>([]),
  nextCollection = ref<number | null>(null)
const selectedBinding = ref<ZoteroBinding | null>(null),
  items = ref<ZoteroItem[]>([]),
  nextItem = ref<number | null>(null)
const versions = ref<
  Record<
    string,
    { items: { remote_version: number; source_revision_id: string }[]; next_offset: number | null }
  >
>({})
const states: Record<string, string> = {
  pending: '待采纳',
  invalid: '需校正',
  needs_resolution: '待解析 DOI',
  conflict: '元数据冲突',
  duplicate: '已有相同条目',
  identity_conflict: '身份冲突',
  imported: '已采纳',
  skipped: '已跳过',
  active: '可同步',
  paused: '已暂停',
  revoked: '已撤销本机连接',
  needs_configuration: '待重新配置',
  available: '可用',
  not_in_selected_collection: '已不在所选资料集',
}
async function run(name: string, work: () => Promise<void>) {
  if (busy.value) return
  busy.value = name
  error.value = ''
  notice.value = ''
  try {
    await work()
  } catch (e) {
    error.value = sourceError(e)
  } finally {
    busy.value = ''
  }
}
async function refresh() {
  await ensureSourceConversation(cid.value)
  const [a, b] = await Promise.all([api.batches(cid.value), api.bindings(cid.value)])
  batches.value = a.items
  bindings.value = b.items
  if (selectedBinding.value)
    selectedBinding.value = b.items.find((x) => x.id === selectedBinding.value?.id) || null
}
async function pick(event: Event) {
  const target = event.target as HTMLInputElement,
    file = target.files?.[0]
  target.value = ''
  if (!file) return
  await run('file', async () => {
    if (file.size > 4 * 1024 * 1024) throw new Error('每个书目文件最多4MiB')
    const suffix = file.name.split('.').pop()?.toLowerCase()
    if (!['bib', 'ris', 'txt'].includes(suffix || ''))
      throw new Error('请选择 .bib、.ris 或 DOI 文本文件')
    content.value = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
    title.value = file.name
    format.value = suffix === 'ris' ? 'ris' : suffix === 'bib' ? 'bibtex' : 'doi'
  })
}
async function preview() {
  await run('preview', async () => {
    await ensureSourceConversation(cid.value)
    const signature = JSON.stringify([title.value, format.value, content.value])
    if (signature !== requestSignature) {
      requestId = crypto.randomUUID()
      requestSignature = signature
    }
    batch.value = await api.preview({
      conversation_id: cid.value,
      request_id: requestId,
      title: title.value,
      format: format.value,
      content: content.value,
    })
    await refresh()
  })
}
async function openBatch(id: string) {
  if (id)
    await run('open', async () => {
      batch.value = await api.batch(id, cid.value)
    })
}
async function entry(row: BibliographyEntry, operation: string, action?: string) {
  await run(row.id, async () => {
    const updated = await api.entry(row.id, operation, {
      conversation_id: cid.value,
      expected_version: row.version,
      ...(action ? { action } : {}),
    })
    if (batch.value)
      batch.value.items = batch.value.items.map((x) => (x.id === updated.id ? updated : x))
    if (updated.paper_id) emit('changed')
  })
}
async function loadCollections(more = false) {
  await run('collections', async () => {
    await ensureSourceConversation(cid.value)
    const result = await api.collections({
      conversation_id: cid.value,
      user_id: userId.value.trim(),
      api_key: apiKey.value,
      start: more ? nextCollection.value || 0 : 0,
    })
    collections.value = more ? [...collections.value, ...result.items] : result.items
    nextCollection.value = result.next_start
    if (!collections.value.some((x) => x.key === collection.value)) collection.value = ''
  })
}
async function connect() {
  await run('connect', async () => {
    await api.connect({
      conversation_id: cid.value,
      user_id: userId.value.trim(),
      api_key: apiKey.value,
      collection_key: collection.value,
    })
    apiKey.value = ''
    await refresh()
    notice.value = '已保存本机连接；同步会先生成书目预览。'
  })
}
async function sync(binding: ZoteroBinding) {
  await run('sync:' + binding.id, async () => {
    try {
      const result = await api.sync(binding.id, {
        conversation_id: cid.value,
        expected_version: binding.version,
      })
      notice.value = `同步完成，${result.changed_items} 个条目或附件有新版本。`
      if (result.batch_id) batch.value = await api.batch(result.batch_id, cid.value)
      selectedBinding.value = result.binding
      const resultItems = await api.items(binding.id, cid.value)
      items.value = resultItems.items
      nextItem.value = resultItems.next_offset
    } finally {
      await refresh()
    }
  })
}
// A pause/revoke remains operable while another request is reading remotely.
async function state(binding: ZoteroBinding, action: string) {
  try {
    await api.state(binding.id, {
      conversation_id: cid.value,
      expected_version: binding.version,
      action,
    })
    await refresh()
  } catch (e) {
    error.value = sourceError(e)
  }
}
async function showItems(binding: ZoteroBinding, more = false) {
  await run('items', async () => {
    selectedBinding.value = binding
    const result = await api.items(binding.id, cid.value, more ? nextItem.value || 0 : 0)
    items.value = more ? [...items.value, ...result.items] : result.items
    nextItem.value = result.next_offset
  })
}
async function file(item: ZoteroItem, operation: string) {
  const b = selectedBinding.value
  if (!b) return
  await run(item.id, async () => {
    const result = await api.file(b.id, item.id, operation, {
      conversation_id: cid.value,
      expected_version: b.version,
      remote_version: item.version,
    })
    if (result.revision_id) item.source_revision_id = result.revision_id
    if (result.paper_id) {
      emit('changed')
      notice.value = '全文已附加到原书目记录，阅读状态以文献库解析结果为准。'
    }
  })
}
function read(rid: string) {
  void router.push(sourceReaderLocation(rid, cid.value))
}
async function history(item: ZoteroItem, more = false) {
  const b = selectedBinding.value
  if (!b) return
  await run('versions', async () => {
    const result = await api.versions(
      b.id,
      item.id,
      cid.value,
      more ? versions.value[item.id]?.next_offset || 0 : 0
    )
    versions.value[item.id] = {
      ...result,
      items: more ? [...(versions.value[item.id]?.items || []), ...result.items] : result.items,
    }
  })
}
onMounted(() => run('load', refresh))
</script>

<template>
  <section class="bibliography-panel" aria-label="书目与 Zotero 导入">
    <div class="bib-head">
      <div>
        <h2>书目与 Zotero</h2>
        <p>先核对条目，再逐份采纳。没有全文的书目会清楚标为“仅书目”。</p>
      </div>
    </div>
    <p v-if="error" class="bib-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="bib-notice" role="status">{{ notice }}</p>
    <div class="bib-controls">
      <label
        >导入类型<select v-model="format" aria-label="书目格式">
          <option value="bibtex">BibTeX</option>
          <option value="ris">RIS</option>
          <option value="doi">DOI（每行一个）</option>
        </select></label
      >
      <label>批次名称<input v-model="title" maxlength="200" aria-label="书目批次名称" /></label>
      <button class="el-button" :disabled="!!busy" @click="fileInput?.click()">选择书目文件</button>
      <input ref="fileInput" type="file" accept=".bib,.ris,.txt" hidden @change="pick" />
    </div>
    <textarea
      v-model="content"
      aria-label="书目原始内容"
      rows="5"
      placeholder="粘贴 BibTeX、RIS，或每行一个 DOI；仅在点击“解析 DOI”时请求 Crossref。"
    ></textarea>
    <div class="bib-controls">
      <button
        class="el-button el-button--primary"
        :disabled="!!busy || !content.trim()"
        @click="preview"
      >
        {{ busy === 'preview' ? '正在预览…' : '保存并预览书目' }}
      </button>
      <label
        >已保存批次<select
          aria-label="打开书目批次"
          :value="batch?.id || ''"
          :disabled="!!busy"
          @change="openBatch(($event.target as HTMLSelectElement).value)"
        >
          <option value="">选择已保存批次</option>
          <option v-for="b in batches" :key="b.id" :value="b.id">
            {{ b.title }} · {{ b.format }}
          </option>
        </select></label
      >
    </div>
    <div v-if="batch" class="bib-preview" aria-label="书目预览">
      <h3>{{ batch.title }} · {{ batch.items.length }} 条</h3>
      <p v-if="!batch.items.length">此批次仅更新附件版本，没有新的父书目需要采纳。</p>
      <article v-for="row in batch.items" :key="row.id" class="bib-entry" :data-entry="row.id">
        <div class="bib-entry-title">
          <strong>{{ row.metadata.title || row.metadata.doi || `条目 ${row.position + 1}` }}</strong
          ><span>{{ states[row.status] || row.status }}</span>
        </div>
        <p>
          {{ row.metadata.authors || '作者未提供' }} · {{ row.metadata.published_at || '年份未提供'
          }}<template v-if="row.metadata.doi"> · DOI: {{ row.metadata.doi }}</template>
        </p>
        <p v-if="row.error" class="bib-error">{{ row.error }}</p>
        <details v-if="row.existing_metadata">
          <summary>核对已有书目元数据</summary>
          <p>{{ row.existing_metadata.title }}</p>
          <p>{{ row.existing_metadata.authors }} · {{ row.existing_metadata.published_at }}</p>
          <p>已有全文与阅读证据不会被本次元数据选择替换。</p>
        </details>
        <details>
          <summary>保留的原始条目</summary>
          <pre>{{ row.raw_entry }}</pre>
        </details>
        <div v-if="!row.decision" class="bib-controls">
          <button
            v-if="row.metadata.doi"
            class="el-button"
            :disabled="!!busy"
            @click="entry(row, 'resolve')"
          >
            解析 DOI / 重试
          </button>
          <button
            v-if="row.status === 'pending'"
            class="el-button"
            :disabled="!!busy"
            @click="entry(row, 'decision', 'create')"
          >
            采纳书目
          </button>
          <template v-if="['conflict', 'duplicate'].includes(row.status)"
            ><button class="el-button" :disabled="!!busy" @click="entry(row, 'decision', 'keep')">
              保留已有元数据</button
            ><button class="el-button" :disabled="!!busy" @click="entry(row, 'decision', 'update')">
              采用本次元数据
            </button></template
          >
          <button class="el-button" :disabled="!!busy" @click="entry(row, 'refresh')">
            重新检查重复项
          </button>
          <button class="el-button" :disabled="!!busy" @click="entry(row, 'decision', 'skip')">
            跳过此条
          </button>
        </div>
      </article>
    </div>
    <details class="zotero-setup">
      <summary>连接 Zotero 个人资料库</summary>
      <p>
        使用 Zotero 官方 Web API
        v3。仅从所选资料集读取书目和附件；不会远程写入或删除。此入口使用个人 API 密钥，不是浏览器
        OAuth。
      </p>
      <div class="bib-controls">
        <label
          >用户 ID<input v-model="userId" aria-label="Zotero 用户 ID" inputmode="numeric" /></label
        ><label
          >API 密钥<input
            v-model="apiKey"
            type="password"
            autocomplete="off"
            aria-label="Zotero API 密钥" /></label
        ><button
          class="el-button"
          :disabled="!!busy || !apiKey || !userId"
          @click="loadCollections(false)"
        >
          读取资料集
        </button>
      </div>
      <div class="bib-controls">
        <label
          >选择资料集<select v-model="collection" aria-label="Zotero 资料集">
            <option value="">请选择一个资料集</option>
            <option v-for="c in collections" :key="c.key" :value="c.key">{{ c.name }}</option>
          </select></label
        ><button
          v-if="nextCollection !== null"
          class="el-button"
          :disabled="!!busy"
          @click="loadCollections(true)"
        >
          更多资料集</button
        ><button class="el-button" :disabled="!!busy || !collection || !apiKey" @click="connect">
          保存连接
        </button>
      </div>
      <p>
        密钥由 Windows
        当前用户保护。普通备份排除凭据，恢复后需重新连接；撤销本机连接后，已保存的文献和固定原件继续保留。
      </p>
    </details>
    <article v-for="binding in bindings" :key="binding.id" class="zotero-binding">
      <strong>{{ binding.collection_name }}</strong
      ><span>
        · {{ states[binding.status] || binding.status }} · 已保存远端版本 {{ binding.cursor }}</span
      >
      <p v-if="binding.last_error" class="bib-error">{{ binding.last_error }}</p>
      <div class="bib-controls">
        <button
          class="el-button"
          :disabled="!!busy || binding.status !== 'active'"
          @click="sync(binding)"
        >
          同步并预览</button
        ><button class="el-button" :disabled="!!busy" @click="showItems(binding)">
          条目与附件
        </button>
        <button
          v-if="binding.status === 'active'"
          class="el-button"
          @click="state(binding, 'pause')"
        >
          暂停连接</button
        ><button
          v-if="binding.status === 'paused'"
          class="el-button"
          @click="state(binding, 'resume')"
        >
          恢复连接
        </button>
        <button
          v-if="binding.status !== 'revoked'"
          class="el-button"
          @click="state(binding, 'revoke')"
        >
          撤销本机连接
        </button>
      </div>
    </article>
    <div v-if="selectedBinding" class="zotero-items">
      <h3>{{ selectedBinding.collection_name }} · 条目与附件</h3>
      <p>
        远端删除或移出资料集只更新这里的状态，本地文献仍保留。附件需逐份保存，全文需另外选择附加到已采纳的父书目。
      </p>
      <article v-for="item in items" :key="item.id" class="bib-entry">
        <strong>{{ item.title }}</strong>
        <p>
          {{ states[item.state] || item.state }} · 远端 v{{ item.version }} ·
          {{
            item.item_type === 'attachment'
              ? item.source_revision_id
                ? '已保存固定原件'
                : '尚未取得全文'
              : '书目元数据'
          }}
        </p>
        <div
          v-if="item.item_type === 'attachment' && item.content_type === 'application/pdf'"
          class="bib-controls"
        >
          <button
            v-if="!item.source_revision_id"
            class="el-button"
            :disabled="!!busy || selectedBinding.status !== 'active' || item.state !== 'available'"
            @click="file(item, 'download')"
          >
            保存此版 PDF
          </button>
          <template v-else
            ><button class="el-button" @click="read(item.source_revision_id)">打开固定原件</button
            ><button
              class="el-button"
              :disabled="!!busy || item.state !== 'available'"
              @click="file(item, 'attach-fulltext')"
            >
              附加全文到父书目
            </button></template
          >
          <button class="el-button" :disabled="!!busy" @click="history(item)">
            查看已保存版本
          </button>
        </div>
        <div v-if="versions[item.id]" class="bib-controls">
          <span v-if="!versions[item.id].items.length">尚无保存的原件版本</span
          ><button
            v-for="saved in versions[item.id].items"
            :key="saved.source_revision_id"
            class="el-button"
            @click="read(saved.source_revision_id)"
          >
            原件 v{{ saved.remote_version }}</button
          ><button
            v-if="versions[item.id].next_offset !== null"
            class="el-button"
            :disabled="!!busy"
            @click="history(item, true)"
          >
            更早版本
          </button>
        </div>
      </article>
      <button
        v-if="nextItem !== null"
        class="el-button"
        :disabled="!!busy"
        @click="showItems(selectedBinding, true)"
      >
        更多条目
      </button>
    </div>
  </section>
</template>

<style scoped>
.bibliography-panel {
  border: 1px solid var(--fn-border);
  border-radius: 12px;
  padding: 20px;
  margin: 18px 0;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
h2 {
  font-size: 18px;
  margin: 0 0 6px;
}
h3 {
  font-size: 15px;
  margin: 14px 0;
}
p {
  font-size: 13px;
  line-height: 1.65;
  color: var(--fn-text-muted);
  margin: 6px 0;
}
.bib-controls {
  display: flex;
  align-items: end;
  gap: 9px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.bib-controls label {
  display: grid;
  gap: 5px;
  font-size: 12px;
}
.bib-controls input,
.bib-controls select,
textarea {
  font: inherit;
  border: 1px solid var(--fn-border);
  border-radius: 6px;
  padding: 8px;
  color: var(--fn-text-primary);
  background: var(--fn-bg-primary);
}
textarea {
  width: 100%;
  resize: vertical;
  box-sizing: border-box;
  min-height: 100px;
}
.bib-error {
  color: var(--el-color-danger);
  overflow-wrap: anywhere;
}
.bib-notice {
  color: var(--el-color-success);
}
.bib-entry {
  border-top: 1px solid var(--fn-border);
  padding: 14px 0;
}
.bib-entry-title {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  font-size: 14px;
}
.bib-entry-title span {
  flex-shrink: 0;
  color: var(--fn-text-muted);
  font-size: 12px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  max-height: 220px;
  overflow: auto;
  padding: 10px;
  background: var(--fn-bg-secondary);
}
summary {
  cursor: pointer;
  font-size: 13px;
  padding: 6px 0;
}
.zotero-setup,
.zotero-binding,
.zotero-items {
  border-top: 1px solid var(--fn-border);
  margin-top: 18px;
  padding-top: 14px;
}
.zotero-binding {
  font-size: 13px;
}
.el-button {
  margin-left: 0;
  white-space: normal;
}
.bib-preview {
  max-height: 650px;
  overflow: auto;
  padding: 0 6px;
}
</style>
