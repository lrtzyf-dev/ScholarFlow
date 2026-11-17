<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ChevronRight, Info, Search } from 'lucide-vue-next'
import { pagePapers } from '../api'
import { zones, type NavView } from '../navConfig'
import { activeConversationId } from '../desktopState'
import { taskState } from '../taskState'
import ContextConversations from './ContextConversations.vue'
import ProjectSidebar from './ProjectSidebar.vue'

const props = defineProps<{ activeViewName: string }>()
const emit = defineEmits<{
  (e: 'navigate', view: NavView): void
  (e: 'collapse'): void
  (e: 'openPalette'): void
  (e: 'newConversation'): void
}>()
const route = useRoute()
const primaryViews = zones
  .find((zone) => zone.id === 'library')!
  .views.filter((view) => view.name !== 'reader')
const toolZones = zones.filter((zone) => ['analysis', 'writing'].includes(zone.id))
const systemViews = zones
  .find((zone) => zone.id === 'system')!
  .views.filter((view) => view.name !== 'about')
const aboutView = zones
  .find((zone) => zone.id === 'system')!
  .views.find((view) => view.name === 'about')!
const opened = ref<string[]>([])
const currentId = computed(() => String(route.query.conv || activeConversationId.value || ''))
const paperCount = ref<number | null>(null)
async function refreshPaperCount() {
  try {
    paperCount.value = (await pagePapers({limit:1})).statistics.papers
  } catch {
    paperCount.value = null
  }
}
onMounted(refreshPaperCount)
watch(
  () => route.path,
  (_next, previous) => {
    if (previous === '/library') void refreshPaperCount()
  }
)
watch(
  () => props.activeViewName,
  (name) => {
    const zone = toolZones.find((item) => item.views.some((view) => view.name === name))
    if (zone && !opened.value.includes(zone.id)) opened.value.push(zone.id)
  },
  { immediate: true }
)
function toggleTools(id: string) {
  opened.value = opened.value.includes(id)
    ? opened.value.filter((value) => value !== id)
    : [...opened.value, id]
}
</script>

<template>
  <aside class="research-sidebar" aria-label="主导航">
    <div class="research-brand-row">
      <button
        class="research-brand"
        type="button"
        aria-label="研序问答工作台"
        @click="emit('navigate', zones[0].views[0])"
      >
        <img class="brand-icon" src="/app-icon.png" alt="研序" />
        <span>研序 <small>ScholarFlow</small></span>
      </button>
      <button
        class="sidebar-icon"
        type="button"
        title="收起侧栏（Ctrl+B）"
        aria-label="收起侧栏"
        @click="emit('collapse')"
      >
        <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <path d="M9 4v16" />
        </svg>
      </button>
    </div>

    <div class="sidebar-scroll">
      <nav class="primary-navigation" aria-label="主要功能">
        <button type="button" class="research-nav new-research" @click="emit('newConversation')">
          <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="m14 4 6 6M4 20l5-1L21 7l-5-5L4 14z" /></svg
          ><span>新建研究</span><kbd>Ctrl Shift O</kbd>
        </button>
        <button type="button" class="research-nav" @click="emit('openPalette')">
          <Search :size="17" :stroke-width="1.65" /><span>搜索</span><kbd>Ctrl K</kbd>
        </button>
        <button
          v-for="view in primaryViews"
          :key="view.name"
          type="button"
          class="research-nav"
          :aria-current="activeViewName === view.name ? 'page' : undefined"
          @click="emit('navigate', view)"
        >
          <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path
              :d="
                view.name === 'library'
                  ? 'M4 4h5v16H4zM9 4h5v16H9zM17 4l4 15-4 1-4-15'
                  : 'M5 12a7 7 0 0 1 7-7m-7 3V4h4M19 12a7 7 0 0 1-7 7m7-3v4h-4'
              "
            /></svg
          ><span>{{ view.label }}</span
          ><span v-if="view.name === 'library' && paperCount !== null" class="nav-count">{{
            paperCount
          }}</span>
        </button>
      </nav>

      <section class="research-section">
        <div class="section-heading">研究工具</div>
        <div v-for="zone in toolZones" :key="zone.id" class="tool-group">
          <button
            type="button"
            class="research-nav"
            :aria-expanded="opened.includes(zone.id)"
            :aria-controls="`research-tools-${zone.id}`"
            @click="toggleTools(zone.id)"
          >
            <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
              <template v-if="zone.id === 'analysis'">
                <circle cx="6" cy="17" r="2.5" />
                <circle cx="18" cy="7" r="2.5" />
                <path d="M8 15 16 9M4 5v16h17" />
              </template>
              <path v-else d="M12 20h9M4 17l-1 4 4-1L20 7l-3-3zM14 7l3 3" />
            </svg>
            <span>{{ zone.id === 'analysis' ? '文献分析' : '写作辅助' }}</span>
            <ChevronRight
              :size="13"
              class="tool-chevron"
              :class="{ expanded: opened.includes(zone.id) }"
            />
          </button>
          <nav
            v-show="opened.includes(zone.id)"
            :id="`research-tools-${zone.id}`"
            class="tool-items"
            :aria-label="zone.label"
          >
            <button
              v-for="view in zone.views"
              :key="view.name"
              type="button"
              class="research-nav"
              :aria-current="activeViewName === view.name ? 'page' : undefined"
              @click="emit('navigate', view)"
            >
              <span>{{ view.label }}</span>
            </button>
          </nav>
        </div>
      </section>

      <ProjectSidebar class="research-projects" :current-id="currentId" />
      <ContextConversations class="research-conversations" />
    </div>

    <div class="sidebar-footer">
      <nav aria-label="系统功能">
        <button
          v-for="view in systemViews"
          :key="view.name"
          type="button"
          class="research-nav"
          :aria-current="activeViewName === view.name ? 'page' : undefined"
          @click="emit('navigate', view)"
        >
          <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
            <template v-if="view.name === 'tasks'">
              <rect x="4" y="3" width="16" height="18" rx="2" />
              <path d="m7 8 1 1 2-2m-3 8 1 1 2-2m3-5h4m-4 7h4" />
            </template>
            <template v-else>
              <path d="M3 7h18M3 17h18" />
              <circle cx="8" cy="7" r="3" fill="var(--surface)" />
              <circle cx="16" cy="17" r="3" fill="var(--surface)" />
            </template></svg
          ><span>{{ view.name === 'settings' ? '设置' : view.label }}</span>
          <span
            v-if="view.name === 'tasks' && taskState.pendingApprovals.value"
            class="approval-count"
            >{{ taskState.pendingApprovals.value }}</span
          >
        </button>
      </nav>
      <div class="workspace-identity">
        <span class="workspace-avatar" aria-hidden="true">研</span>
        <div>个人研究空间<small>本地优先 · 科研工作台</small></div>
        <button
          type="button"
          class="sidebar-icon"
          aria-label="关于研序"
          title="关于研序"
          @click="emit('navigate', aboutView)"
        >
          <Info :size="17" />
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.reference-icon {
  width: 17px;
  height: 17px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.65;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.research-brand .brand-icon {
  width: 32px;
  height: 32px;
  object-fit: contain;
  flex-shrink: 0;
}
.sidebar-icon .reference-icon {
  width: 18px;
  height: 18px;
}
.nav-count {
  margin-left: auto;
  font-size: 11px;
  color: var(--fn-text-secondary);
}
.research-sidebar {
  flex: 0 0 var(--sidebar-width, 244px);
  width: var(--sidebar-width, 244px);
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 20px 12px 12px;
  background: var(--sidebar, #edf1f7);
  border-right: 1px solid var(--fn-border-primary);
}
.research-brand-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 9px 24px;
  flex-shrink: 0;
}
.research-brand {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--fn-text-primary);
  font-size: 19px;
  font-weight: 600;
  letter-spacing: -0.5px;
  white-space: nowrap;
  cursor: pointer;
}
.research-brand svg {
  color: var(--fn-brand);
  flex-shrink: 0;
}
.research-brand small {
  font-size: 11px;
  font-weight: 400;
  color: var(--fn-text-secondary);
  margin-left: 2px;
  letter-spacing: 0;
}
.sidebar-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 8px;
  background: none;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.sidebar-icon:hover {
  background: var(--hover, #e6ecf8);
  color: var(--fn-text-primary);
}
.sidebar-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  scrollbar-width: thin;
  scrollbar-gutter: stable;
}
.research-nav {
  position: relative;
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  min-height: 37px;
  margin-bottom: 2px;
  padding: 9px 12px;
  border: 0;
  border-radius: 8px;
  background: none;
  color: var(--nav-text, #38435c);
  font: inherit;
  font-size: var(--text-base, 14px);
  text-align: left;
  cursor: pointer;
}
.research-nav > svg {
  color: var(--nav-icon, #5e6b83);
  flex-shrink: 0;
}
.research-nav:hover {
  background: var(--hover, #e6ecf8);
}
.research-nav:active {
  background: var(--selected, #e4ebff);
}
.research-nav[aria-current='page'] {
  color: var(--fn-brand-hover);
  background: var(--selected, #e4ebff);
}
.research-nav[aria-current='page']::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 3px;
  background: var(--fn-brand);
}
.research-nav[aria-current='page'] > svg {
  color: var(--fn-brand);
}
.research-nav kbd {
  margin-left: auto;
  border: 0;
  color: var(--fn-text-secondary);
  font-size: 10px;
  font-family: var(--font-mono, Consolas, monospace);
  white-space: nowrap;
}
.new-research {
  min-height: 40px;
  margin-bottom: 11px;
  background: var(--fn-brand);
  color: var(--fn-on-brand);
  font-weight: 600;
}
.new-research > svg,
.new-research kbd {
  color: inherit;
}
.new-research:hover {
  background: var(--fn-brand-hover);
}
.new-research:active {
  background: var(--fn-brand-active);
}
.research-section {
  margin-top: 24px;
}
.section-heading {
  padding: 0 12px 7px;
  color: var(--fn-text-secondary);
  font-size: var(--text-xs, 12px);
}
.tool-chevron {
  margin-left: auto;
  transition: transform var(--fn-transition-fast);
}
.tool-chevron.expanded {
  transform: rotate(90deg);
}
.tool-items {
  margin: 0 10px 7px 21px;
  padding-left: 16px;
  border-left: 1px solid var(--nav-line, #d7e0ef);
}
.tool-items .research-nav {
  min-height: 32px;
  padding: 6px 9px;
  font-size: var(--text-sm, 13px);
}
.research-projects,
.research-conversations {
  margin-top: 24px;
}
.sidebar-footer {
  flex-shrink: 0;
  padding-top: 8px;
}
.approval-count {
  margin-left: auto;
  min-width: 18px;
  padding: 0 4px;
  border-radius: 4px;
  font-size: 11px;
  text-align: center;
  color: var(--fn-warning-text);
  background: var(--fn-warning-bg);
}
.workspace-identity {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  border-top: 1px solid var(--fn-border-primary);
  padding: 15px 9px 2px;
  font-size: 12px;
}
.workspace-avatar {
  width: 29px;
  height: 29px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: var(--fn-brand);
  background: var(--selected, #e4ebff);
  border: 1px solid var(--control-border, #ced8ea);
  border-radius: 50%;
}
.workspace-identity small {
  display: block;
  margin-top: 2px;
  font-size: 10px;
  color: var(--fn-text-secondary);
}
.workspace-identity .sidebar-icon {
  margin-left: auto;
}
button:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
@media (max-width: 760px) {
  .research-sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 30;
  }
}
</style>
