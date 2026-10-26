<script setup lang="ts">
import { computed } from 'vue'

import { useAppStore } from '@/stores/app'

const store = useAppStore()

const label = computed(() => {
  if (store.healthState === 'online') return store.health?.env ? `在线 · ${store.health.env}` : '服务在线'
  if (store.healthState === 'checking') return '检测中'
  return '服务不可达'
})

const tooltip = computed(() => {
  if (store.healthState === 'online') {
    return `${store.health?.app ?? 'InterviewForge'} · ${store.health?.env ?? ''} · 点击重新检测`
  }
  if (store.healthState === 'checking') return '正在请求 /health'
  return `${store.healthError || '无法连接服务'} · 点击重试`
})
</script>

<template>
  <el-tooltip :content="tooltip" placement="bottom">
    <button class="health-badge" :class="store.healthState" @click="store.checkHealth()">
      <span class="health-dot" aria-hidden="true" />
      <span>{{ label }}</span>
    </button>
  </el-tooltip>
</template>

<style scoped>
.health-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 11px;
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
  cursor: pointer;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: 999px;
  transition: border-color 0.15s ease;
}

.health-badge:hover {
  border-color: var(--line-strong);
}

.health-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-3);
}

.health-badge.online {
  color: var(--ok);
  background: var(--ok-soft);
  border-color: color-mix(in srgb, var(--ok) 20%, transparent);
}

.health-badge.online .health-dot {
  background: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 18%, transparent);
}

.health-badge.offline {
  color: var(--bad);
  background: var(--bad-soft);
  border-color: color-mix(in srgb, var(--bad) 20%, transparent);
}

.health-badge.offline .health-dot {
  background: var(--bad);
}

.health-badge.checking .health-dot {
  background: var(--warn);
  animation: pulse 1.1s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}
</style>
