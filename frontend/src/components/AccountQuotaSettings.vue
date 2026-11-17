<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import axios from 'axios'
import {
  bindQuotaAccount,
  createQuotaGroup,
  loadAccountQuotas,
  updateQuotaGroup,
  type AccountQuotas,
  type QuotaAccount,
  type QuotaGroup,
} from '../quotaApi'

const value = ref<AccountQuotas | null>(null)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const stale = ref(false)
let generation = 0
const create = reactive({ name: '', origin: '', concurrency: 2 })
const drafts = reactive<Record<string, Pick<QuotaGroup, 'name' | 'concurrency' | 'state'>>>({})
const targets = reactive<Record<string, string>>({})
const pending = ref<{ account: QuotaAccount; target: QuotaGroup } | null>(null)

function accept(next: AccountQuotas, reset = false) {
  const previous = value.value
  for (const group of next.groups) {
    const old = previous?.groups.find((g) => g.id === group.id)
    const draft = drafts[group.id]
    const dirty =
      old &&
      draft &&
      (draft.name !== old.name ||
        draft.concurrency !== old.concurrency ||
        draft.state !== old.state)
    if (reset || !dirty)
      drafts[group.id] = { name: group.name, concurrency: group.concurrency, state: group.state }
  }
  for (const account of next.accounts) {
    const old = previous?.accounts.find((a) => a.id === account.id)
    const selected = next.groups.find(
      (g) => g.id === targets[account.id] && g.origin === account.origin && g.state === 'active'
    )
    if (reset || old?.config_fingerprint !== account.config_fingerprint || !selected)
      targets[account.id] = next.groups.some(
        (g) => g.id === account.group_id && g.state === 'active'
      )
        ? account.group_id || ''
        : ''
  }
  value.value = next
  stale.value = false
  pending.value = null
}
function message(e: unknown) {
  if (axios.isAxiosError(e)) {
    if (e.response?.status === 409) stale.value = true
    return typeof e.response?.data?.detail === 'string'
      ? e.response.data.detail
      : '账号配额设置未保存，请核对后重试'
  }
  return e instanceof Error ? e.message : '账号配额设置未保存'
}
async function perform(action: () => Promise<AccountQuotas>, success = '') {
  const current = ++generation
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const next = await action()
    if (current !== generation) return
    accept(next, action === loadAccountQuotas)
    notice.value = success
  } catch (e) {
    if (current === generation) error.value = message(e)
  } finally {
    if (current === generation) busy.value = false
  }
}
function valid(name: string, concurrency: number) {
  if (!name.trim() || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 64) {
    error.value = '请填写名称和1到64之间的整数并发数'
    return false
  }
  return true
}
async function add() {
  if (!value.value || !valid(create.name, create.concurrency)) return
  await perform(
    () => createQuotaGroup(value.value!.revision, { ...create }),
    '账号配额组已创建，请在下方明确选择需要归组的连接'
  )
}
async function save(group: QuotaGroup) {
  const draft = drafts[group.id]
  if (!value.value || !draft || !valid(draft.name, draft.concurrency)) return
  await perform(
    () => updateQuotaGroup(group.id, value.value!.revision, { ...draft }),
    '配额设置已保存；新限额在请求派发时生效，已经在途的请求继续完成'
  )
}
function choose(account: QuotaAccount) {
  const target = value.value?.groups.find((g) => g.id === targets[account.id])
  if (target && target.id !== account.group_id) pending.value = { account, target }
}
async function confirm() {
  if (!pending.value || !value.value) return
  const selected = pending.value
  await perform(
    () => bindQuotaAccount(selected.target.id, value.value!.revision, selected.account),
    '连接归组已保存；新任务和恢复的旧任务都使用该账号当前分组'
  )
}
function groupName(id: string | null) {
  return value.value?.groups.find((g) => g.id === id)?.name || '未单独归组'
}
onMounted(() => perform(loadAccountQuotas))
onBeforeUnmount(() => {
  generation++
})
</script>
<template>
  <section class="account-quotas" aria-labelledby="account-quota-heading">
    <div class="quota-heading">
      <h3 id="account-quota-heading">账号配额与并发</h3>
      <button type="button" :disabled="busy" @click="perform(loadAccountQuotas)">
        刷新配额设置
      </button>
    </div>
    <p>
      同一账号的不同密钥可以明确归为一组，共用并发额度。接口地址和密钥相同的模型、Embedding
      与工具请求原本就共享额度；不会按模型名称猜测账号归属。
    </p>
    <p v-if="error" class="quota-error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="stale" class="quota-error">保存前请刷新并核对当前配置；刷新会重新载入下方分组字段。</p>
    <template v-if="value">
      <p class="quota-note">
        未归组连接默认并发
        {{
          value.default_concurrency
        }}。同组会话轮流派发，每个会话同时最多一个请求；前台、后台和子任务共用额度。以下在途与排队数量以本次刷新为准。
      </p>
      <form class="quota-create" @submit.prevent="add">
        <label for="quota-new-name"
          >分组名称<input
            id="quota-new-name"
            v-model="create.name"
            maxlength="120"
            placeholder="例如：实验室 DeepSeek 账号"
            required
        /></label>
        <label for="quota-new-origin"
          >接口来源<input
            id="quota-new-origin"
            v-model="create.origin"
            placeholder="https://api.deepseek.com"
            list="quota-origins"
            required
        /></label>
        <datalist id="quota-origins">
          <option
            v-for="item in [...new Set(value.accounts.map((a) => a.origin))]"
            :key="item"
            :value="item"
          />
        </datalist>
        <label for="quota-new-limit"
          >最大并发<input
            id="quota-new-limit"
            v-model.number="create.concurrency"
            type="number"
            min="1"
            max="64"
            step="1"
            required
        /></label>
        <button :disabled="busy || stale">创建账号组</button>
      </form>
      <p v-if="!value.groups.length" class="quota-note">
        尚未建立单独的账号组。添加组后，再选择需要共同计入额度的已保存连接。
      </p>
      <article
        v-for="group in value.groups"
        :key="group.id"
        class="quota-group"
        :aria-label="`账号组 ${group.name}`"
      >
        <div class="quota-group-summary">
          <strong>{{ group.name }}</strong
          ><span>{{ group.origin }}</span
          ><span
            >在途 {{ group.active }} · 排队 {{ group.queued }} · 限流等待
            {{ group.cooldown_seconds }} 秒 · 凭据成员 {{ group.member_count }}</span
          >
        </div>
        <form class="quota-edit" @submit.prevent="save(group)">
          <label :for="`quota-name-${group.id}`"
            >名称<input
              :id="`quota-name-${group.id}`"
              v-model="drafts[group.id]!.name"
              maxlength="120"
              required
          /></label>
          <label :for="`quota-limit-${group.id}`"
            >并发数<input
              :id="`quota-limit-${group.id}`"
              v-model.number="drafts[group.id]!.concurrency"
              type="number"
              min="1"
              max="64"
              step="1"
              required
          /></label>
          <label :for="`quota-state-${group.id}`"
            >分组状态<select
              :id="`quota-state-${group.id}`"
              v-model="drafts[group.id]!.state"
              aria-label="分组状态"
            >
              <option value="active">启用</option>
              <option value="retired">退休，保留现有额度约束</option>
            </select></label
          >
          <button :disabled="busy || stale">保存账号组</button>
        </form>
        <small v-if="group.state === 'retired'"
          >此组保留历史和现有绑定，不接受新归组；仍属于它的连接继续使用该组限额，不会因退休绕过约束。</small
        >
      </article>
      <h4>已保存的连接</h4>
      <p v-if="!value.accounts.length">
        暂无可归组的已保存凭据。先在模型或工具设置中保存可用连接，然后刷新。
      </p>
      <article
        v-for="account in value.accounts"
        :key="account.id"
        class="quota-account"
        :aria-label="`连接 ${account.name}`"
      >
        <div>
          <strong>{{ account.name }}</strong
          ><small>{{ account.origin }} · 当前：{{ groupName(account.group_id) }}</small
          ><small v-if="account.same_credential_accounts.length > 1"
            >此凭据同时用于：{{
              account.same_credential_accounts.join('、')
            }}；归组会共同生效。</small
          >
        </div>
        <label :for="`quota-target-${account.id}`"
          >目标账号组<select
            :id="`quota-target-${account.id}`"
            v-model="targets[account.id]"
            aria-label="目标账号组"
          >
            <option value="">选择同来源账号组</option>
            <option
              v-for="group in value.groups.filter(
                (g) => g.origin === account.origin && g.state === 'active'
              )"
              :key="group.id"
              :value="group.id"
            >
              {{ group.name }} · 并发 {{ group.concurrency }}
            </option>
          </select></label
        >
        <button
          type="button"
          :disabled="
            busy || stale || !targets[account.id] || targets[account.id] === account.group_id
          "
          @click="choose(account)"
        >
          {{ account.group_id ? '审阅迁移' : '审阅归组' }}
        </button>
      </article>
      <div v-if="pending" class="quota-confirm" role="region" aria-label="确认账号归组">
        <p>
          将“{{ pending.account.name }}”从“{{ groupName(pending.account.group_id) }}”归入“{{
            pending.target.name
          }}”，共用 {{ pending.target.concurrency }} 个并发名额。
        </p>
        <p>
          新任务和恢复的旧任务都按当前分组派发。原组或目标组有执行、排队请求或限流冷却时不能迁移，请等待完成；现有请求不会被取消。
        </p>
        <button type="button" :disabled="busy || stale" @click="confirm">确认归组</button
        ><button type="button" :disabled="busy" @click="pending = null">取消</button>
      </div>
    </template>
  </section>
</template>
<style scoped>
.account-quotas {
  border-top: 1px solid var(--border-color, #dfe3ec);
  margin-top: 24px;
  padding-top: 20px;
  line-height: 1.6;
}
.quota-heading,
.quota-group-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 18px;
}
.quota-heading {
  justify-content: space-between;
}
h3,
h4 {
  margin: 0 0 10px;
}
p {
  margin: 8px 0 14px;
}
.quota-create,
.quota-edit,
.quota-account {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) minmax(180px, 2fr) minmax(90px, 0.6fr) auto;
  gap: 12px;
  align-items: end;
}
label {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}
input,
select,
button {
  font: inherit;
  border: 1px solid var(--border-color, #ccd4e4);
  border-radius: 5px;
  background: var(--bg-primary, #fff);
  color: inherit;
  min-height: 34px;
  padding: 5px 9px;
}
input,
select {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
}
button {
  cursor: pointer;
  white-space: nowrap;
}
button:disabled {
  cursor: default;
  opacity: 0.55;
}
input:focus-visible,
select:focus-visible,
button:focus-visible {
  outline: 2px solid var(--primary-color, #315dba);
  outline-offset: 2px;
}
.quota-group {
  border-top: 1px solid var(--border-color, #e1e5ef);
  margin-top: 16px;
  padding: 14px 0;
}
.quota-group-summary {
  margin-bottom: 8px;
}
.quota-group-summary span,
small,
.quota-note {
  color: var(--text-secondary, #64748b);
}
small {
  display: block;
  overflow-wrap: anywhere;
}
.quota-account {
  grid-template-columns: minmax(160px, 2fr) minmax(160px, 1fr) auto;
  border-top: 1px solid var(--border-color, #e1e5ef);
  padding: 12px 0;
}
.quota-confirm {
  border: 1px solid var(--border-color, #ccd4e4);
  border-left: 3px solid var(--primary-color, #315dba);
  padding: 12px;
}
.quota-confirm button + button {
  margin-left: 10px;
}
.quota-error {
  color: var(--danger-color, #b42318);
}
@media (max-width: 850px) {
  .quota-create,
  .quota-edit {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
  .quota-account {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 500px) {
  .quota-create,
  .quota-edit {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
