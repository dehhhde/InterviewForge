<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'

import { useAppStore } from '@/stores/app'

const props = withDefaults(
  defineProps<{
    option: EChartsOption
    height?: number
  }>(),
  { height: 280 },
)

const host = ref<HTMLDivElement | null>(null)
const store = useAppStore()
let chart: echarts.ECharts | null = null

function render(): void {
  if (!host.value) return
  chart ??= echarts.init(host.value)
  chart.setOption(props.option, true)
  chart.resize()
}

function resize(): void {
  chart?.resize()
}

function recreate(): void {
  chart?.dispose()
  chart = null
  void nextTick(render)
}

onMounted(() => {
  render()
  window.addEventListener('resize', resize)
})

watch(() => props.option, render, { deep: true })
watch(() => store.theme, recreate)

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="host" class="chart-box" :style="{ height: `${height}px` }" />
</template>
