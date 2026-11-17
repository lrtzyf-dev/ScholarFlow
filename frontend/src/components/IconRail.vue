<script setup lang="ts">
import { useRouter } from 'vue-router'
import { Bell, CircleHelp } from 'lucide-vue-next'
import { zones, type NavZone } from '../navConfig'

const props = defineProps<{
  activeZoneId: string
  notificationCount?: number
}>()

const router = useRouter()

function goZone(zone: NavZone) {
  if (zone.id === props.activeZoneId) return
  const first = zone.views[0]
  if (first.to) router.push(first.to)
}
</script>

<template>
  <aside class="icon-rail" aria-label="主导航">
    <div class="rail-brand">
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M4 6.5A2.5 2.5 0 0 1 6.5 4H11v15H6.5A2.5 2.5 0 0 0 4 16.5v-10Z"
          fill="var(--fn-brand)"
        />
        <path
          d="M20 6.5A2.5 2.5 0 0 0 17.5 4H13v15h4.5a2.5 2.5 0 0 1 2.5 2.5v-15Z"
          fill="var(--fn-brand)"
          opacity="0.55"
        />
        <path d="M12 5.5v12" stroke="#fff" stroke-width="1.2" />
      </svg>
    </div>

    <nav class="rail-nav">
      <button
        v-for="zone in zones"
        :key="zone.id"
        type="button"
        class="rail-item"
        :class="{ active: zone.id === activeZoneId }"
        :title="zone.label"
        :aria-label="zone.label"
        :aria-current="zone.id === activeZoneId ? 'page' : undefined"
        @click="goZone(zone)"
      >
        <span v-if="zone.id === activeZoneId" class="rail-indicator" aria-hidden="true"></span>
        <component :is="zone.icon" :size="20" :stroke-width="1.8" />
      </button>
    </nav>

    <div class="rail-foot">
      <button type="button" class="rail-item small" title="通知" aria-label="通知">
        <Bell :size="19" :stroke-width="1.8" />
        <span v-if="notificationCount" class="rail-dot" aria-hidden="true"></span>
      </button>
      <button type="button" class="rail-item small" title="帮助" aria-label="帮助">
        <CircleHelp :size="19" :stroke-width="1.8" />
      </button>
    </div>
  </aside>
</template>

<style scoped>
.icon-rail {
  flex: 0 0 56px;
  width: 56px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--fn-bg-primary);
  border-right: 1px solid var(--fn-border-primary);
}
.rail-brand {
  display: flex;
  justify-content: center;
  padding: 12px 0 10px;
}
.rail-brand svg {
  width: 32px;
  height: 32px;
}
.rail-nav {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 4px 0;
  flex: 1;
}
.rail-item {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
  transition:
    background var(--fn-transition-fast),
    color var(--fn-transition-fast);
}
.rail-item:hover {
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
}
.rail-item.active {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.rail-indicator {
  position: absolute;
  left: -6px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 22px;
  border-radius: 0 2px 2px 0;
  background: var(--fn-brand);
}
.rail-foot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 0 12px;
}
.rail-item.small {
  width: 40px;
  height: 40px;
}
.rail-dot {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--fn-danger);
}
</style>
