<script setup lang="ts">
import { computed } from 'vue'

import { SOURCE_LABEL } from '@/utils/format'

const props = defineProps<{ source: string }>()
const label = computed(() => SOURCE_LABEL[props.source] ?? props.source)
</script>

<template>
  <span class="source-tag">{{ label }}</span>
</template>
