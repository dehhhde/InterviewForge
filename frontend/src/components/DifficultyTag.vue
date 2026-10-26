<script setup lang="ts">
import { computed } from 'vue'

import { DIFFICULTY_LABEL } from '@/utils/format'

const props = defineProps<{ difficulty: string }>()
const label = computed(() => DIFFICULTY_LABEL[props.difficulty] ?? props.difficulty)
</script>

<template>
  <span class="diff-tag" :class="difficulty">{{ label }}</span>
</template>
