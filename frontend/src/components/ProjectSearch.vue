<script setup lang="ts">
import { computed, ref, watch, nextTick, onUnmounted } from 'vue'
import { ElDialog } from 'element-plus'
import { Search, CaseSensitive, Regex, WholeWord, SlidersHorizontal, X, RefreshCw, FileText } from 'lucide-vue-next'
import { projectSearchState } from '../desktopState'
import { searchProject, type SearchOptions, type SearchPage, type SearchHit } from '../projectSearchApi'
import { projectError, readProjectFile, type Project, type FilePage } from '../projectApi'

const props = defineProps<{ project: Project; canAttach: boolean; files: string[] }>()
const emit = defineEmits<{ toggle: [path: string] }>()
const draft = computed(() => projectSearchState(props.project.id))
const options = computed(() => draft.value.value.value), saveStatus = computed(() => draft.value.status.value)
const advanced = ref(false), loading = ref(false), error = ref(''), note = ref(''), result = ref<SearchPage | null>(null)
const groups = computed(() => {
  const map = new Map<string, SearchHit[]>()
  for (const hit of result.value?.matches || []) {
    if (!map.has(hit.path)) map.set(hit.path, [])
    map.get(hit.path)!.push(hit)
  }
  return [...map].map(([path, hits]) => ({ path, hits }))
})
const selected = ref<SearchHit | null>(null), preview = ref<FilePage | null>(null), previewError = ref(''), reading = ref(false)
const previewElement = ref<HTMLElement | null>(null)
const previewLength = computed(() => Array.from(preview.value?.text || '').length)
const previewParts = computed(() => {
  if (!preview.value || !selected.value) return { before: '', match: '', after: '', marked: false }
  const text = Array.from(preview.value.text), start = selected.value.offset - preview.value.offset
  if (start < 0 || start > text.length) return { before: text.join(''), match: '', after: '', marked: false }
  const end = start + selected.value.length
  return { before: text.slice(0, start).join(''), match: text.slice(start, end).join(''), after: text.slice(end).join(''), marked: true }
})
let epoch = 0, readEpoch = 0, controller: AbortController | undefined
function cancel() { epoch++; controller?.abort(); controller = undefined; loading.value = false }
function change<K extends keyof SearchOptions>(key: K, value: SearchOptions[K]) {
  cancel(); result.value = null; error.value = ''; note.value = ''
  draft.value.update({ ...options.value, [key]: value })
}
async function search(offset = 0) {
  cancel()
  const request = ++epoch, projectId = props.project.id, settings = { ...options.value }
  if (!settings.query) return
  controller = new AbortController(); loading.value = true; error.value = ''; note.value = ''
  if (!offset) result.value = null
  try {
    const page = await searchProject(projectId, settings, offset, offset ? result.value?.snapshot : undefined, controller.signal)
    if (request !== epoch) return
    if (offset && result.value) page.matches = [...result.value.matches, ...page.matches]
    result.value = page
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) { loading.value = false; controller = undefined } }
}
function closePreview() { selected.value = null; preview.value = null; readEpoch++; reading.value = false }
function scrollToMatch() {
  const container = previewElement.value, mark = container?.querySelector('mark')
  if (container && mark && container.getBoundingClientRect().height) {
    const hit = mark.getBoundingClientRect(), viewport = container.getBoundingClientRect()
    container.scrollTop += hit.top - viewport.top - 80
    container.scrollLeft += hit.left - viewport.left - 40
  }
}
async function open(hit: SearchHit, offset = Math.max(0, hit.offset - 1200)) {
  const request = ++readEpoch, projectId = props.project.id
  selected.value = hit; preview.value = null; previewError.value = ''; reading.value = true
  try {
    const page = await readProjectFile(projectId, hit.path, offset, hit.sha256)
    if (request !== readEpoch) return
    preview.value = page; reading.value = false
    await nextTick()
    if (request !== readEpoch) return
    scrollToMatch()
  } catch (e) { if (request === readEpoch) previewError.value = projectError(e) }
  finally { if (request === readEpoch) reading.value = false }
}
function repeat() { closePreview(); void search() }
function stop() { cancel(); note.value = '已取消搜索' }
watch(() => props.project.id, () => { cancel(); closePreview(); result.value = null; error.value = ''; advanced.value = false })
watch(options, () => { cancel(); result.value = null; error.value = ''; note.value = '' })
watch(() => draft.value.restored.value, () => {
  if (options.value.path || options.value.include || options.value.exclude || options.value.hidden || options.value.ignored) advanced.value = true
}, { immediate: true })
onUnmounted(() => { cancel(); readEpoch++ })
</script>

<template>
  <div class="project-search">
    <form class="search-form" @submit.prevent="search()">
      <div class="search-input-row"><input :value="options.query" aria-label="搜索项目内容" placeholder="搜索项目内容" maxlength="256" :disabled="saveStatus === 'loading'" @input="change('query', ($event.target as HTMLInputElement).value)" /><button type="button" :aria-pressed="options.case_sensitive" title="区分大小写" aria-label="区分大小写" @click="change('case_sensitive', !options.case_sensitive)"><CaseSensitive :size="16" /></button><button type="button" :aria-pressed="options.whole_word" title="整词匹配" aria-label="整词匹配" @click="change('whole_word', !options.whole_word)"><WholeWord :size="16" /></button><button type="button" :aria-pressed="options.regex" title="正则表达式" aria-label="正则表达式" @click="change('regex', !options.regex)"><Regex :size="16" /></button><button type="button" :aria-expanded="advanced" title="搜索范围" aria-label="搜索范围" @click="advanced = !advanced"><SlidersHorizontal :size="16" /></button><button v-if="loading" type="button" title="取消搜索" aria-label="取消搜索" @click="stop"><X :size="16" /></button><button v-else :disabled="!options.query || saveStatus === 'loading'" title="搜索" aria-label="搜索"><Search :size="16" /></button></div>
      <div v-if="advanced" class="search-options"><label>搜索目录<input :value="options.path" aria-label="搜索目录" placeholder="/" @input="change('path', ($event.target as HTMLInputElement).value)" /></label><label>包含文件<input :value="options.include" aria-label="包含文件" placeholder="**/*.py" @input="change('include', ($event.target as HTMLInputElement).value)" /></label><label>排除文件<input :value="options.exclude" aria-label="排除文件" placeholder="**/tests/**" @input="change('exclude', ($event.target as HTMLInputElement).value)" /></label><div class="search-checkboxes"><label><input type="checkbox" :checked="options.hidden" @change="change('hidden', ($event.target as HTMLInputElement).checked)" />隐藏文件</label><label><input type="checkbox" :checked="options.ignored" @change="change('ignored', ($event.target as HTMLInputElement).checked)" />忽略规则中的文件</label></div></div>
    </form>
    <div v-if="saveStatus === 'error' || saveStatus === 'conflict'" class="search-save-error" role="alert"><span>{{ saveStatus === 'conflict' ? '另一窗口已修改搜索条件' : '搜索条件保存失败' }}</span><template v-if="saveStatus === 'conflict'"><button @click="draft.resolveConflict(false)">载入已保存版本</button><button @click="draft.resolveConflict(true)">保留本窗口版本</button></template><button v-else @click="draft.retry()">重试保存</button></div>
    <div class="search-summary" role="status"><span>{{ loading ? '正在搜索…' : note || (result ? `${result.total} 个匹配行，${result.files} 个文件` : '') }}</span><button v-if="result" :disabled="loading" title="刷新搜索" aria-label="刷新搜索" @click="search()"><RefreshCw :size="14" /></button></div>
    <p v-if="error" class="search-error" role="alert">{{ error }} <button :disabled="loading" @click="search()">重新搜索</button></p>
    <p v-if="result?.limited" class="search-warning" role="status">搜索不完整：{{ result.warnings.join('；') }}</p>
    <div class="search-results" :aria-busy="loading">
      <section v-for="group in groups" :key="group.path" class="search-group"><header><input v-if="canAttach" type="checkbox" :aria-label="`引用搜索文件 ${group.path}`" :checked="files.includes(group.path)" @change="emit('toggle', group.path)" /><FileText :size="14" /><strong :title="group.path">{{ group.path }}</strong><span>{{ group.hits.length }}</span></header><button v-for="hit in group.hits" :key="hit.line" class="search-hit" :aria-label="`打开 ${hit.path}:${hit.line}`" @click="open(hit)"><span class="search-line">{{ hit.line }}</span><span class="search-snippet"><template v-for="(part, i) in hit.parts" :key="i"><mark v-if="part.match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></span></button></section>
      <p v-if="result && !result.total && !loading" class="search-empty">{{ result.limited ? '未获得可验证的匹配结果' : '没有匹配结果' }}</p>
      <button v-if="result && result.next_offset !== null" class="search-more" :disabled="loading" @click="search(result.next_offset!)">加载更多匹配</button>
    </div>
  </div>
  <ElDialog :model-value="!!selected" title="搜索文件预览" width="900px" top="5vh" append-to-body :close-on-click-modal="false" @opened="scrollToMatch" @update:model-value="value => { if (!value) closePreview() }">
    <div class="search-preview" @keydown="event => { if (event.ctrlKey || event.metaKey) event.stopPropagation() }"><header><strong>{{ selected?.path }}</strong><span>第 {{ selected?.line }} 行，第 {{ selected?.column }} 列</span></header><div class="search-preview-body"><p v-if="previewError" role="alert" class="search-error">{{ previewError }}</p><p v-if="reading" class="search-empty">正在读取…</p><pre v-else-if="preview" ref="previewElement" tabindex="0" aria-label="搜索文件内容"><span>{{ previewParts.before }}</span><mark v-if="previewParts.marked">{{ previewParts.match }}</mark><span>{{ previewParts.after }}</span></pre></div><div v-if="preview" class="preview-pages"><span>{{ preview.offset + 1 }}–{{ preview.offset + previewLength }} / {{ preview.total_chars }} 字符</span><button :disabled="reading || preview.offset === 0" @click="open(selected!, Math.max(0, preview.offset - 8000))">上一段</button><button :disabled="reading || preview.next_offset === null" @click="open(selected!, preview.next_offset!)">下一段</button></div></div>
    <template #footer><button v-if="previewError" @click="repeat">重新搜索</button><button v-if="selected && preview && canAttach" @click="emit('toggle', selected.path)">{{ files.includes(selected.path) ? '取消引用' : '引用文件' }}</button><button @click="closePreview">关闭</button></template>
  </ElDialog>
</template>

<style scoped>
.project-search { display:flex; flex-direction:column; flex:1; min-height:0; color:var(--fn-text-primary,#202124); }
button { display:inline-flex; align-items:center; justify-content:center; gap:4px; border:1px solid var(--fn-border,#ddd); border-radius:4px; padding:5px 7px; background:transparent; color:inherit; font:inherit; font-size:12px; cursor:pointer; } button:hover { background:var(--fn-bg-hover,#f2f3f4); } button:disabled { opacity:.45; cursor:default; } button:focus-visible,input:focus-visible,pre:focus-visible { outline:2px solid #346ee8; outline-offset:1px; } button[aria-pressed=true],button[aria-expanded=true] { color:#245dcc; border-color:#9bb7f1; background:#edf2fd; }
input:not([type=checkbox]) { min-width:0; width:100%; box-sizing:border-box; padding:6px 7px; border:1px solid var(--fn-border,#ddd); border-radius:4px; background:var(--fn-bg-main,#fff); color:inherit; font:inherit; font-size:12px; }
.search-form { flex:none; padding-top:10px; } .search-input-row { display:flex; align-items:center; gap:4px; } .search-input-row>input { flex:1; } .search-input-row button { width:29px; height:29px; padding:0; flex:none; }
.search-options { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px; padding-top:8px; } .search-options>label { font-size:11px; color:var(--fn-text-secondary,#666); } .search-options input:not([type=checkbox]) { margin-top:3px; } .search-checkboxes { grid-column:1/-1; display:flex; flex-wrap:wrap; gap:12px; padding-top:2px; } .search-checkboxes label { display:flex; align-items:center; gap:4px; font-size:11px; }
.search-summary { display:flex; align-items:center; justify-content:space-between; gap:8px; height:30px; flex:none; font-size:11px; color:var(--fn-text-secondary,#666); } .search-summary button { border:0; } .search-results { flex:1; min-height:0; overflow:auto; }
.search-group { margin-bottom:5px; } .search-group header { display:flex; align-items:center; gap:6px; min-height:28px; font-size:12px; border-bottom:1px solid var(--fn-border,#ddd); } .search-group header svg,.search-group header input { flex:none; } .search-group header strong { flex:1; min-width:0; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .search-group header>span { font-size:11px; color:var(--fn-text-secondary); padding-right:6px; }
.search-hit { display:flex; width:100%; border:0; padding:4px 2px; border-radius:0; text-align:left; min-height:27px; font:12px/19px Consolas,monospace; } .search-line { width:36px; flex:none; text-align:right; padding-right:10px; color:var(--fn-text-secondary,#666); box-sizing:border-box; } .search-snippet { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:pre; } mark { background:#fff0a8; color:inherit; } mark:empty { border-left:2px solid #d58a0a; } .search-more { margin:8px 0; }
.search-error,.search-warning,.search-save-error { flex:none; font-size:12px; overflow-wrap:anywhere; margin:5px 0; } .search-error,.search-save-error { color:#b42318; } .search-warning { color:#925214; } .search-save-error { display:flex; flex-wrap:wrap; align-items:center; gap:6px; } .search-empty { text-align:center; font-size:12px; color:var(--fn-text-secondary,#666); margin:20px 0; }
.search-preview { color:var(--fn-text-primary,#202124); } .search-preview header { display:flex; gap:12px; align-items:center; font-size:12px; } .search-preview header strong { flex:1; min-width:0; overflow-wrap:anywhere; } .search-preview header>span { flex:none; color:var(--fn-text-secondary); } .search-preview-body { height:min(390px,55vh); margin:10px 0; } .search-preview pre { box-sizing:border-box; height:100%; margin:0; overflow:auto; border:1px solid var(--fn-border,#ddd); border-radius:4px; padding:10px; font:12px/20px Consolas,monospace; tab-size:4; white-space:pre; } .preview-pages { display:flex; align-items:center; gap:6px; min-height:27px; font-size:11px; color:var(--fn-text-secondary,#666); } .preview-pages>span { flex:1; min-width:0; }
</style>
