<script setup lang="ts">
import type { Component } from 'vue'

withDefaults(
  defineProps<{
    label: string
    value: string | number
    hint?: string
    tone?: 'brand' | 'green' | 'amber' | 'violet'
    icon?: Component
  }>(),
  { tone: 'brand' },
)
</script>

<template>
  <div class="stat-card">
    <div class="stat-top">
      <span class="stat-label">{{ label }}</span>
      <span v-if="icon" class="stat-icon" :class="tone">
        <el-icon><component :is="icon" /></el-icon>
      </span>
    </div>
    <div>
      <div class="stat-value">{{ value }}</div>
      <p v-if="hint" class="stat-hint">{{ hint }}</p>
    </div>
  </div>
</template>
