<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

defineProps<{
  viewLabel: string
  sectionLabel: string
  modelLabel: string
  modelTitle: string
  panelCollapsed: boolean
  isChat?: boolean
  sourceCount?: number
  sourcesOpen?: boolean
}>()
const emit = defineEmits<{
  (e: 'openPalette'): void
  (e: 'expandPanel'): void
  (e: 'toggleSources'): void
  (e: 'newConversation'): void
}>()
const router = useRouter(),
  route = useRoute()
const menuOpen = ref(false),
  menu = ref<HTMLElement | null>(null),
  menuButton = ref<HTMLButtonElement | null>(null)
function closeMenu(restoreFocus = false) {
  menuOpen.value = false
  if (restoreFocus) menuButton.value?.focus()
}
async function openMenu() {
  if (menuOpen.value) {
    closeMenu(true)
    return
  }
  menuOpen.value = true
  await nextTick()
  menu.value?.querySelector<HTMLButtonElement>('button')?.focus()
}
function menuKeys(event: KeyboardEvent) {
  const buttons = [...(menu.value?.querySelectorAll<HTMLButtonElement>('button') || [])]
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeMenu(true)
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length
    buttons[next]?.focus()
  } else if (event.key === 'Tab') closeMenu()
}
function activate(action: string) {
  closeMenu()
  if (action === 'search') emit('openPalette')
  else if (action === 'new') emit('newConversation')
  else if (action === 'memory') window.dispatchEvent(new Event('scholarflow:open-memory'))
  else void router.push(action)
}
function outside(event: PointerEvent) {
  if (
    menuOpen.value &&
    !menu.value?.contains(event.target as Node) &&
    !menuButton.value?.contains(event.target as Node)
  )
    closeMenu()
}
watch(
  () => route.fullPath,
  () => closeMenu()
)
onMounted(() => document.addEventListener('pointerdown', outside))
onUnmounted(() => document.removeEventListener('pointerdown', outside))
</script>

<template>
  <header class="top-bar">
    <div class="bar-left">
      <button
        v-if="panelCollapsed"
        type="button"
        class="bar-icon"
        title="展开侧栏（Ctrl+B）"
        aria-label="展开侧栏"
        @click="emit('expandPanel')"
      >
        <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <path d="M9 4v16" />
        </svg>
      </button>
      <span class="bar-section">{{ sectionLabel === '问答' ? '科研问答' : sectionLabel }}</span>
      <span class="bar-divider" aria-hidden="true">/</span>
      <span class="bar-breadcrumb">{{ viewLabel }}</span>
    </div>
    <div class="bar-right">
      <span class="model-mark" :title="modelTitle">{{ modelLabel }}</span>
      <button
        v-if="isChat"
        type="button"
        class="sources-button"
        :aria-expanded="sourcesOpen"
        aria-controls="research-inspector"
        @click="emit('toggleSources')"
      >
        <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M5 4h7l2 2h5v15H5zM8 10h8M8 14h8M8 18h5" /></svg
        >资料 <span>{{ sourceCount || 0 }}</span>
      </button>
      <button
        ref="menuButton"
        type="button"
        class="bar-icon"
        aria-label="更多操作"
        title="更多操作"
        aria-haspopup="menu"
        aria-controls="research-more-menu"
        :aria-expanded="menuOpen"
        @click="openMenu"
        @keydown.down.prevent="openMenu"
      >
        <svg class="reference-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="5" cy="12" r="1" />
          <circle cx="12" cy="12" r="1" />
          <circle cx="19" cy="12" r="1" />
        </svg>
      </button>
    </div>
    <div
      v-if="menuOpen"
      id="research-more-menu"
      ref="menu"
      class="more-menu"
      role="menu"
      aria-label="更多操作"
      @keydown="menuKeys"
    >
      <button type="button" role="menuitem" @click="activate('new')">新建研究</button>
      <button type="button" role="menuitem" @click="activate('search')">搜索页面与工具</button>
      <button type="button" role="menuitem" @click="activate('/chat?history=1')">
        历史研究会话
      </button>
      <button v-if="isChat" type="button" role="menuitem" @click="activate('memory')">
        会话记忆
      </button>
      <button type="button" role="menuitem" @click="activate('/settings')">模型与设置</button>
      <button type="button" role="menuitem" @click="activate('/about')">关于研序</button>
    </div>
  </header>
</template>

<style scoped>
.top-bar {
  position: relative;
  height: var(--topbar-height, 56px);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 27px;
  border-bottom: 1px solid var(--fn-border-primary);
  gap: 15px;
  background: var(--fn-bg-primary);
}
.bar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  font-size: 12px;
}
.bar-section {
  color: var(--fn-text-secondary);
  white-space: nowrap;
}
.bar-divider {
  color: var(--nav-line, #d7e0ef);
}
.bar-breadcrumb {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.bar-right {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}
.bar-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 0;
  background: transparent;
  border-radius: 8px;
  color: var(--fn-text-secondary);
  flex-shrink: 0;
  cursor: pointer;
}
.bar-icon:hover {
  background: var(--hover, #e6ecf8);
  color: var(--fn-text-primary);
}
.reference-icon {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.65;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.model-mark {
  font-size: 10px;
  padding: 3px 7px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
  white-space: nowrap;
}
.sources-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 0;
  min-height: 30px;
  padding: 7px 13px;
  border-radius: 8px;
  font-size: 12px;
  line-height: var(--leading-ui, 1.5);
  font-weight: 500;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.sources-button:hover {
  background: var(--fn-bg-secondary);
}
.sources-button .reference-icon {
  width: 15px;
  height: 15px;
}
.sources-button span {
  color: var(--fn-text-tertiary);
}
.more-menu {
  position: absolute;
  right: 24px;
  top: 49px;
  z-index: 50;
  min-width: 180px;
  padding: 5px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
  box-shadow: var(--shadow-menu);
}
.more-menu button {
  width: 100%;
  border: 0;
  background: none;
  text-align: left;
  padding: 9px 10px;
  font: inherit;
  font-size: 12px;
  border-radius: 5px;
  cursor: pointer;
}
.more-menu button:hover,
.more-menu button:focus-visible {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
button:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 3px;
}
@media (max-width: 1250px) {
  .top-bar {
    padding: 0 20px;
  }
  .bar-section,
  .bar-divider {
    display: none;
  }
}
@media (max-width: 1050px) {
  .model-mark {
    display: none;
  }
}
@media (max-width: 760px) {
  .top-bar {
    padding: 0 12px;
    gap: 8px;
  }
  .bar-left {
    gap: 6px;
  }
  .bar-right {
    gap: 2px;
  }
  .sources-button {
    font-size: 11px;
    padding: 5px 7px;
  }
  .more-menu {
    right: 12px;
  }
}
</style>
