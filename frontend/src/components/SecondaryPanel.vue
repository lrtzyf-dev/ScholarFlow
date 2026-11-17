<script setup lang="ts">
import { ChevronsLeft } from 'lucide-vue-next'
import type { NavZone, NavView } from '../navConfig'

defineProps<{
  zone: NavZone
  activeViewName: string
  /** 是否使用上下文态（默认插槽）而非菜单态 */
  hasContext?: boolean
}>()

const emit = defineEmits<{
  (e: 'navigate', view: NavView): void
  (e: 'collapse'): void
}>()
</script>

<template>
  <aside class="secondary-panel" aria-label="上下文导航">
    <div class="panel-head">
      <span class="panel-title">{{ zone.label }}</span>
      <button
        type="button"
        class="panel-collapse"
        title="收起面板"
        aria-label="收起面板"
        @click="emit('collapse')"
      >
        <ChevronsLeft :size="16" :stroke-width="1.8" />
      </button>
    </div>

    <div class="panel-body">
      <slot v-if="hasContext" />
      <template v-else>
        <button
          v-for="view in zone.views"
          :key="view.name"
          type="button"
          class="panel-item"
          :class="{ active: view.name === activeViewName }"
          @click="emit('navigate', view)"
        >
          <component :is="view.icon" :size="16" :stroke-width="1.8" class="panel-item-ico" />
          <span class="panel-item-label">{{ view.label }}</span>
        </button>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.secondary-panel {
  flex: 0 0 224px;
  width: 224px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--fn-bg-primary);
  border-right: 1px solid var(--fn-border-primary);
}
.panel-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 48px;
  padding: 0 12px 0 16px;
}
.panel-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.panel-collapse {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: pointer;
}
.panel-collapse:hover {
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
}
.panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 4px 10px 12px;
}
.panel-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 36px;
  padding: 0 12px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
  text-align: left;
  transition:
    background var(--fn-transition-fast),
    color var(--fn-transition-fast);
}
.panel-item:hover {
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
}
.panel-item.active {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
  font-weight: 600;
}
.panel-item-ico {
  flex: none;
}
.panel-item-label {
  flex: 1;
  font-size: 13.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
