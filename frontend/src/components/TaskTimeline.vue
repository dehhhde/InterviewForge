<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Timer } from '@element-plus/icons-vue'

import type { TaskStatus, TaskStep } from '@/api/types'
import {
  NODE_DESCRIPTION,
  formatDuration,
  formatMs,
  nodeLabel,
  pipelineSize,
} from '@/utils/format'

const props = withDefaults(
  defineProps<{
    steps?: TaskStep[]
    status: TaskStatus
    startedAt?: number | null
    finishedAt?: number | null
  }>(),
  { steps: () => [], startedAt: null, finishedAt: null },
)

interface StepRow {
  node: string
  label: string
  description: string
  count: number
  ms: number
  round: number
  order: number
  fan: boolean
  redo: boolean
}

const now = ref(Date.now())
let timer: number | undefined

onMounted(() => {
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
})
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})

const rows = computed<StepRow[]>(() => {
  const seen = new Map<string, number>()
  const output: StepRow[] = []

  for (const [index, step] of props.steps.entries()) {
    const round = (seen.get(step.node) ?? 0) + 1
    seen.set(step.node, round)
    const previous = output[output.length - 1]
    if (previous && previous.node === step.node) {
      previous.count += 1
      previous.ms += step.ms ?? 0
      continue
    }
    output.push({
      node: step.node,
      label: nodeLabel(step.node),
      description: NODE_DESCRIPTION[step.node] ?? '',
      count: 1,
      ms: step.ms ?? 0,
      round,
      order: index + 1,
      fan: step.node === 'generate_item',
      redo: round > 1 && step.node !== 'generate_item',
    })
  }
  return output
})

const progress = computed(() => {
  if (props.status === 'completed') return 100
  const nodes = new Set(
    props.steps.filter((step) => step.node !== 'generate_item').map((step) => step.node),
  )
  return Math.min(95, Math.round((nodes.size / pipelineSize()) * 100))
})

const elapsed = computed(() => {
  if (!props.startedAt) return '—'
  const end = props.finishedAt ?? now.value
  return formatDuration(end - props.startedAt)
})

const progressColor = computed(() => {
  if (props.status === 'failed') return 'var(--bad)'
  if (props.status === 'completed') return 'var(--ok)'
  return 'var(--brand)'
})
</script>

<template>
  <div>
    <div class="task-summary">
      <div class="task-summary-left">
        <span class="tiny">执行进度</span>
        <strong>{{ progress }}%</strong>
      </div>
      <span class="task-elapsed">
        <el-icon><Timer /></el-icon>
        {{ elapsed }}
      </span>
    </div>

    <el-progress
      :percentage="progress"
      :stroke-width="6"
      :show-text="false"
      :color="progressColor"
    />

    <div class="task-timeline">
      <div v-if="!rows.length" class="task-waiting">
        <span class="task-waiting-dot" />
        任务已受理，等待第一个 super-step…
      </div>
      <div
        v-for="(row, index) in rows"
        :key="`${row.node}-${index}`"
        class="task-step"
        :class="{ 'is-fan': row.fan, 'is-redo': row.redo }"
      >
        <div class="task-dot">{{ row.fan ? '∥' : row.order }}</div>
        <div class="task-main">
          <div class="task-name">
            {{ row.label }}
            <span v-if="row.count > 1" class="count-chip">×{{ row.count }}</span>
            <span v-if="row.fan" class="pill live">并行扇出</span>
            <span v-else-if="row.round > 1" class="pill warn">第 {{ row.round }} 轮</span>
          </div>
          <div class="task-meta">
            {{ row.description }}
            <span v-if="row.ms"> · {{ formatMs(row.ms) }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.task-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.task-summary-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.task-summary-left strong {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

.task-timeline {
  margin-top: 18px;
}

.task-waiting {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 12.5px;
  color: var(--text-3);
}

.task-waiting-dot {
  width: 7px;
  height: 7px;
  background: var(--brand);
  border-radius: 50%;
  animation: pulse 1.1s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.3;
  }
}
</style>
