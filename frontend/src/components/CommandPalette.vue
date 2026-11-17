<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Search, CornerDownLeft } from 'lucide-vue-next'
import { allViews, type NavView } from '../navConfig'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'navigate', view: NavView): void
}>()

const query = ref('')
const selectedIndex = ref(0)
const inputEl = ref<HTMLInputElement | null>(null)

interface FlatItem {
  zoneLabel: string
  view: NavView
}

const items = computed<FlatItem[]>(() => {
  const q = query.value.trim().toLowerCase()
  return allViews
    .filter(({ view }) => !q || view.label.toLowerCase().includes(q))
    .map(({ zone, view }) => ({ zoneLabel: zone.label, view }))
})

watch(
  () => props.open,
  async (open) => {
    if (open) {
      query.value = ''
      selectedIndex.value = 0
      await nextTick()
      inputEl.value?.focus()
    }
  }
)

watch(query, () => {
  selectedIndex.value = 0
})

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
    return
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (items.value.length) selectedIndex.value = (selectedIndex.value + 1) % items.value.length
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (items.value.length)
      selectedIndex.value = (selectedIndex.value - 1 + items.value.length) % items.value.length
    return
  }
  if (e.key === 'Enter') {
    e.preventDefault()
    const item = items.value[selectedIndex.value]
    if (item) emit('navigate', item.view)
  }
}

function choose(view: NavView) {
  emit('navigate', view)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="palette-overlay" @click.self="emit('close')">
      <div class="palette" role="dialog" aria-modal="true" aria-label="命令面板">
        <div class="palette-input-wrap">
          <Search :size="16" class="palette-input-ico" />
          <input
            ref="inputEl"
            v-model="query"
            type="text"
            class="palette-input"
            placeholder="搜索功能并回车跳转…"
            @keydown="onKeydown"
          />
        </div>

        <div class="palette-list">
          <button
            v-for="(item, index) in items"
            :key="item.view.name"
            type="button"
            class="palette-item"
            :class="{ active: index === selectedIndex }"
            @mouseenter="selectedIndex = index"
            @click="choose(item.view)"
          >
            <component
              :is="item.view.icon"
              :size="16"
              :stroke-width="1.8"
              class="palette-item-ico"
            />
            <span class="palette-item-label">{{ item.view.label }}</span>
            <span class="palette-item-zone">{{ item.zoneLabel }}</span>
            <CornerDownLeft v-if="index === selectedIndex" :size="14" class="palette-item-enter" />
          </button>
          <div v-if="!items.length" class="palette-empty">没有匹配的功能</div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.palette-overlay {
  position: fixed;
  inset: 0;
  z-index: 4000;
  background: rgba(17, 24, 39, 0.28);
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding-top: 12vh;
}
.palette {
  width: 560px;
  max-width: 92vw;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 14px;
  box-shadow: var(--fn-shadow-xl);
  overflow: hidden;
}
.palette-input-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.palette-input-ico {
  color: var(--fn-text-tertiary);
  flex: none;
}
.palette-input {
  flex: 1;
  border: none;
  outline: none;
  font-size: 14px;
  color: var(--fn-text-primary);
  background: transparent;
}
.palette-list {
  max-height: 340px;
  overflow-y: auto;
  padding: 8px;
}
.palette-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 40px;
  padding: 0 12px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
  text-align: left;
}
.palette-item.active {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.palette-item-ico {
  flex: none;
}
.palette-item-label {
  font-size: 13.5px;
}
.palette-item-zone {
  margin-left: auto;
  font-size: 11px;
  color: var(--fn-text-tertiary);
  padding: 1px 8px;
  border-radius: 9999px;
  background: var(--fn-bg-secondary);
}
.palette-item-enter {
  flex: none;
}
.palette-empty {
  padding: 24px;
  text-align: center;
  font-size: 13px;
  color: var(--fn-text-tertiary);
}
</style>
