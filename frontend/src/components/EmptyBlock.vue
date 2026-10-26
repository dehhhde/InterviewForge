<script setup lang="ts">
import type { Component } from 'vue'
import { Files } from '@element-plus/icons-vue'

withDefaults(
  defineProps<{
    title: string
    description?: string
    icon?: Component
  }>(),
  { icon: Files },
)
</script>

<template>
  <div class="empty-block">
    <div class="empty-icon">
      <el-icon><component :is="icon" /></el-icon>
    </div>
    <strong>{{ title }}</strong>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.default" style="margin-top: 8px">
      <slot />
    </div>
  </div>
</template>
