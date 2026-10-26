<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowRight,
  Briefcase,
  Document,
  MagicStick,
  Refresh,
  Tickets,
  TrendCharts,
} from '@element-plus/icons-vue'
import type { EChartsOption } from 'echarts'

import { getInterview, listInterviews, listJobs, listResumes } from '@/api'
import type { Interview, Job, Resume } from '@/api/types'
import ChartPanel from '@/components/ChartPanel.vue'
import EmptyBlock from '@/components/EmptyBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatCard from '@/components/StatCard.vue'
import StatusPill from '@/components/StatusPill.vue'
import { useAppStore } from '@/stores/app'
import {
  DIFFICULTY_LABEL,
  NODE_DESCRIPTION,
  NODE_LABEL,
  PIPELINE,
  formatDateTime,
  reportPairLabel,
  shortId,
} from '@/utils/format'

const router = useRouter()
const store = useAppStore()

const resumes = ref<Resume[]>([])
const jobs = ref<Job[]>([])
const interviews = ref<Interview[]>([])
const details = ref<Interview[]>([])
const loading = ref(true)

const sortedReports = computed(() =>
  [...interviews.value].sort((left, right) => right.created_at.localeCompare(left.created_at)),
)

const runningCount = computed(
  () => interviews.value.filter((report) => report.status === 'running').length,
)

const totalQuestions = computed(() =>
  details.value.reduce((total, report) => total + report.questions.length, 0),
)

const issueTotal = computed(() =>
  interviews.value.reduce((total, report) => total + report.review_issues.length, 0),
)

const difficultyCounts = computed(() => {
  const counts = { easy: 0, medium: 0, hard: 0 }
  for (const report of details.value) {
    for (const question of report.questions) counts[question.difficulty] += 1
  }
  return counts
})

const trend = computed(() => {
  const days: string[] = []
  const labels: string[] = []
  for (let offset = 6; offset >= 0; offset -= 1) {
    const date = new Date()
    date.setDate(date.getDate() - offset)
    days.push(date.toISOString().slice(0, 10))
    labels.push(`${date.getMonth() + 1}/${date.getDate()}`)
  }
  const counts = new Map(days.map((day) => [day, 0]))
  for (const report of interviews.value) {
    const key = report.created_at.slice(0, 10)
    if (counts.has(key)) counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return { labels, values: days.map((day) => counts.get(day) ?? 0) }
})

const axisText = computed(() => (store.isDark ? '#6f7c95' : '#8b96ac'))
const axisLine = computed(() => (store.isDark ? '#22304a' : '#e8edf6'))
const surfaceColor = computed(() => (store.isDark ? '#111a2b' : '#ffffff'))

const trendOption = computed<EChartsOption>(() => ({
  grid: { left: 6, right: 12, top: 18, bottom: 6, containLabel: true },
  tooltip: { trigger: 'axis' },
  xAxis: {
    type: 'category',
    data: trend.value.labels,
    axisTick: { show: false },
    axisLine: { lineStyle: { color: axisLine.value } },
    axisLabel: { color: axisText.value },
  },
  yAxis: {
    type: 'value',
    minInterval: 1,
    axisLabel: { color: axisText.value },
    splitLine: { lineStyle: { color: axisLine.value, type: 'dashed' } },
  },
  series: [
    {
      type: 'bar',
      data: trend.value.values,
      barWidth: 20,
      itemStyle: { color: '#5b7cfa', borderRadius: [6, 6, 0, 0] },
    },
  ],
}))

const difficultyOption = computed<EChartsOption>(() => ({
  tooltip: { trigger: 'item', formatter: '{b}：{c} 题（{d}%）' },
  legend: { bottom: 0, icon: 'circle', textStyle: { color: axisText.value } },
  series: [
    {
      type: 'pie',
      radius: ['50%', '72%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: surfaceColor.value, borderWidth: 3 },
      label: { show: false },
      data: [
        { name: DIFFICULTY_LABEL.easy, value: difficultyCounts.value.easy, itemStyle: { color: '#35c48f' } },
        { name: DIFFICULTY_LABEL.medium, value: difficultyCounts.value.medium, itemStyle: { color: '#5b7cfa' } },
        { name: DIFFICULTY_LABEL.hard, value: difficultyCounts.value.hard, itemStyle: { color: '#f2a33c' } },
      ],
    },
  ],
}))

async function load(): Promise<void> {
  loading.value = true
  try {
    const [resumeRows, jobRows, interviewRows] = await Promise.all([
      listResumes(200),
      listJobs(200),
      listInterviews(200),
    ])
    resumes.value = resumeRows
    jobs.value = jobRows
    interviews.value = interviewRows

    const recent = interviewRows.filter((report) => report.status === 'completed').slice(0, 8)
    const settled = await Promise.allSettled(recent.map((report) => getInterview(report.id)))
    details.value = settled
      .filter((item): item is PromiseFulfilledResult<Interview> => item.status === 'fulfilled')
      .map((item) => item.value)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function openReport(report: Interview): void {
  void router.push(`/reports/${report.id}`)
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="page">
    <PageHeader
      eyebrow="OVERVIEW"
      title="工作台"
      subtitle="简历解析、岗位匹配与 AI 出题的统一入口。所有数据都来自当前 SQLite 业务库，报告头部保存生成当时的简历与岗位快照。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="MagicStick" @click="router.push('/generate')">
          发起出题
        </el-button>
      </template>
    </PageHeader>

    <div class="grid grid-4" v-loading="loading">
      <StatCard
        label="简历"
        :value="resumes.length"
        :hint="`${resumes.filter((resume) => resume.name).length} 份已完成结构化抽取`"
        tone="brand"
        :icon="Tickets"
      />
      <StatCard
        label="岗位"
        :value="jobs.length"
        hint="JD 技能由确定性词典派生"
        tone="violet"
        :icon="Briefcase"
      />
      <StatCard
        label="报告"
        :value="interviews.length"
        :hint="`最近 ${details.length} 份累计 ${totalQuestions} 道题`"
        tone="green"
        :icon="Document"
      />
      <StatCard
        label="进行中"
        :value="runningCount"
        :hint="`历史批注 ${issueTotal} 条（只标注，不拦截交付）`"
        tone="amber"
        :icon="TrendCharts"
      />
    </div>

    <div class="grid grid-2">
      <div class="card" v-loading="loading">
        <div class="card-head">
          <div>
            <h3>近 7 天报告量</h3>
            <p>按创建时间统计，含运行中与已中止的报告。</p>
          </div>
          <span class="pill muted">近 7 天</span>
        </div>
        <ChartPanel :option="trendOption" :height="250" />
      </div>

      <div class="card" v-loading="loading">
        <div class="card-head">
          <div>
            <h3>题目难度分布</h3>
            <p>取最近最多 8 份已完成报告的题目。</p>
          </div>
          <span class="pill muted">{{ totalQuestions }} 题</span>
        </div>
        <ChartPanel v-if="totalQuestions" :option="difficultyOption" :height="250" />
        <EmptyBlock
          v-else
          title="暂无题目数据"
          description="完成一次出题后，这里会显示最近报告的难度构成。"
        />
      </div>
    </div>

    <div class="card flush" v-loading="loading">
      <div class="card-head" style="padding: 20px 22px 0; margin-bottom: 10px">
        <div>
          <h3>最近报告</h3>
          <p>标题来自报告头的快照列：简历改名、岗位下线都不会改写历史。</p>
        </div>
        <el-button text type="primary" @click="router.push('/reports')">
          全部报告 <el-icon><ArrowRight /></el-icon>
        </el-button>
      </div>
      <el-table
        :data="sortedReports.slice(0, 6)"
        row-class-name="clickable-row"
        @row-click="openReport"
      >
        <el-table-column label="简历 × 岗位" min-width="300">
          <template #default="{ row }">
            <div class="table-title">{{ reportPairLabel(row) }}</div>
            <div class="table-sub">
              {{ shortId(row.id, 12) }} · {{ formatDateTime(row.created_at) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }"><StatusPill :status="row.status" /></template>
        </el-table-column>
        <el-table-column label="题数" width="90">
          <template #default="{ row }">
            <span class="mono">{{ row.total_questions }}</span>
          </template>
        </el-table-column>
        <el-table-column label="批注" width="90">
          <template #default="{ row }">
            <span v-if="row.review_issues.length" class="pill warn">
              {{ row.review_issues.length }}
            </span>
            <span v-else class="tiny">—</span>
          </template>
        </el-table-column>
        <el-table-column width="50" align="right">
          <template #default><el-icon><ArrowRight /></el-icon></template>
        </el-table-column>
        <template #empty>
          <EmptyBlock
            title="还没有报告"
            description="选一份简历与一个岗位，发起第一次出题。"
          >
            <el-button type="primary" size="small" @click="router.push('/generate')">
              发起出题
            </el-button>
          </EmptyBlock>
        </template>
      </el-table>
    </div>

    <div class="card">
      <div class="card-head">
        <div>
          <h3>七节点工作流</h3>
          <p>图在后台运行，页面每 2 秒轮询一次任务进度；题目只在评审收尾一次性落库。</p>
        </div>
        <span class="pill live">LangGraph</span>
      </div>
      <div class="workflow-strip">
        <div v-for="(node, index) in PIPELINE" :key="node" class="workflow-node">
          <b>{{ index + 1 }}. {{ NODE_LABEL[node] }}</b>
          <span>{{ NODE_DESCRIPTION[node] }}</span>
          <i>→</i>
        </div>
      </div>
    </div>
  </div>
</template>
