<script setup lang="ts">
import { computed } from 'vue'

import { STATUS_META } from '@/utils/format'

const props = defineProps<{ status: string }>()

const meta = computed(
  () => STATUS_META[props.status] ?? { label: props.status, tone: 'muted' as const },
)
</script>

<template>
  <span class="pill" :class="meta.tone">
    <i class="pill-dot" aria-hidden="true" />
    {{ meta.label }}
  </span>
</template>
