<script setup lang="ts">
defineProps<{
  eyebrow?: string
  title: string
  subtitle?: string
}>()
</script>

<template>
  <div class="page-head">
    <div class="page-head-text">
      <span v-if="eyebrow" class="page-eyebrow">{{ eyebrow }}</span>
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="subtitle" class="page-sub">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.actions" class="page-actions">
      <slot name="actions" />
    </div>
  </div>
</template>
