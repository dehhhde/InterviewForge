<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft,
  Download,
  InfoFilled,
  Printer,
  Refresh,
  WarningFilled,
} from '@element-plus/icons-vue'

import { getInterview } from '@/api'
import { isApiFailure } from '@/api/http'
import type { Interview } from '@/api/types'
import EmptyBlock from '@/components/EmptyBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import ReportViewer from '@/components/ReportViewer.vue'
import StatusPill from '@/components/StatusPill.vue'
import {
  DIFFICULTY_LABEL,
  formatDateTime,
  reportPairLabel,
  shortId,
} from '@/utils/format'
import { downloadMarkdown } from '@/utils/markdown'

const route = useRoute()
const router = useRouter()

const report = ref<Interview | null>(null)
const loading = ref(true)
const notFound = ref(false)
const errorMessage = ref('')
const exporting = ref(false)

const quotas = computed(() => {
  const source = report.value?.plan?.difficulty_quotas ?? {}
  return Object.entries(source)
    .filter(([, value]) => typeof value === 'number')
    .map(([key, value]) => ({ key, label: DIFFICULTY_LABEL[key] ?? key, value: value as number }))
})

const planItems = computed(() => report.value?.plan?.items ?? [])

async function load(): Promise<void> {
  const id = String(route.params.id ?? '')
  loading.value = true
  notFound.value = false
  errorMessage.value = ''
  try {
    report.value = await getInterview(id)
  } catch (error) {
    report.value = null
    if (isApiFailure(error) && error.status === 404) {
      notFound.value = true
    } else {
      errorMessage.value = (error as Error).message
      ElMessage.error(errorMessage.value)
    }
  } finally {
    loading.value = false
  }
}

async function exportMarkdown(): Promise<void> {
  if (!report.value) return
  exporting.value = true
  try {
    downloadMarkdown(report.value)
  } finally {
    exporting.value = false
  }
}

function printReport(): void {
  window.print()
}

watch(() => route.params.id, () => void load())

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="page">
    <PageHeader
      :eyebrow="report ? `REPORT · ${shortId(report.id, 12)}` : 'REPORT'"
      :title="report ? reportPairLabel(report) : '报告详情'"
      :subtitle="
        report
          ? `生成于 ${formatDateTime(report.created_at)} · 最后更新 ${formatDateTime(report.updated_at)}。报告头的简历与岗位是生成当时的快照。`
          : '按报告 id 读取完整题目与评审批注。'
      "
    >
      <template #actions>
        <el-button :icon="ArrowLeft" @click="router.push('/reports')">返回列表</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button :icon="Download" :disabled="!report" :loading="exporting" @click="exportMarkdown">
          导出 Markdown
        </el-button>
        <el-button type="primary" :icon="Printer" :disabled="!report" @click="printReport">
          打印
        </el-button>
      </template>
    </PageHeader>

    <div v-if="loading" class="card">
      <el-skeleton :rows="10" animated />
    </div>

    <div v-else-if="notFound" class="card">
      <EmptyBlock
        :icon="WarningFilled"
        title="报告不存在"
        :description="`没有找到 id 为 ${route.params.id} 的报告。它可能已被删除，或链接不完整。`"
      >
        <el-button type="primary" size="small" @click="router.push('/reports')">
          回到报告中心
        </el-button>
      </EmptyBlock>
    </div>

    <div v-else-if="errorMessage" class="card">
      <EmptyBlock
        :icon="WarningFilled"
        title="读取报告失败"
        :description="errorMessage"
      >
        <el-button size="small" :icon="Refresh" @click="load">重试</el-button>
      </EmptyBlock>
    </div>

    <template v-else-if="report">
      <div class="card">
        <div class="report-headline">
          <div>
            <div class="chip-row">
              <StatusPill :status="report.status" />
              <span class="pill muted">{{ report.questions.length }} 题</span>
              <span class="pill" :class="report.review_issues.length ? 'warn' : 'ok'">
                {{ report.review_issues.length ? `${report.review_issues.length} 条批注` : '无批注' }}
              </span>
            </div>
            <p class="hint" style="margin-top: 10px">
              <el-icon class="inline-icon"><InfoFilled /></el-icon>
              简历与岗位名来自报告头的快照列，不是当前简历库里的值；改名或下线都不会回写这里。
            </p>
          </div>
          <div class="contact-card">
            <div class="kv-row">
              <span>简历</span>
              <span>{{ report.resume_name || '未记录姓名' }}</span>
            </div>
            <div class="kv-row">
              <span>简历文件</span>
              <span>{{ report.resume_file_name || '—' }}</span>
            </div>
            <div class="kv-row">
              <span>岗位</span>
              <span>{{ report.job_title || '未记录岗位' }}</span>
            </div>
            <div class="kv-row">
              <span>公司</span>
              <span>{{ report.job_company || '—' }}</span>
            </div>
          </div>
        </div>

        <div class="divider" />

        <div class="meta-grid">
          <div>
            <span class="section-label" style="margin-top: 0">报告 id</span>
            <code>{{ report.id }}</code>
          </div>
          <div>
            <span class="section-label" style="margin-top: 0">简历 id</span>
            <code>{{ report.resume_id }}</code>
          </div>
          <div>
            <span class="section-label" style="margin-top: 0">岗位 id</span>
            <code>{{ report.job_id || '已下线（SET NULL）' }}</code>
          </div>
          <div>
            <span class="section-label" style="margin-top: 0">生成时间</span>
            <span class="tiny">{{ formatDateTime(report.created_at) }}</span>
          </div>
        </div>
      </div>

      <div v-if="report.plan?.strategy || quotas.length || planItems.length" class="card">
        <div class="card-head">
          <div>
            <h3>出题计划</h3>
            <p>
              难度配比与槽位在 <code>plan</code> 节点确定，扇出出题时每个槽位一个分支；
              这里只做展示，不提供重跑入口。
            </p>
          </div>
          <span v-if="report.plan?.total" class="pill muted">
            计划 {{ report.plan.total }} 题
          </span>
        </div>

        <div v-if="report.plan?.strategy" class="plan-strategy">
          {{ report.plan.strategy }}
        </div>

        <div v-if="quotas.length" class="chip-row" style="margin-top: 12px">
          <span v-for="quota in quotas" :key="quota.key" class="skill-chip">
            {{ quota.label }} <b>{{ quota.value }}</b>
          </span>
        </div>

        <el-table v-if="planItems.length" :data="planItems" size="small" style="margin-top: 14px">
          <el-table-column label="主题" min-width="200">
            <template #default="{ row }">{{ row.topic || '未命名主题' }}</template>
          </el-table-column>
          <el-table-column label="难度" width="100">
            <template #default="{ row }">
              {{ row.difficulty ? DIFFICULTY_LABEL[row.difficulty] ?? row.difficulty : '—' }}
            </template>
          </el-table-column>
          <el-table-column label="题数" width="80">
            <template #default="{ row }">{{ row.count ?? '—' }}</template>
          </el-table-column>
          <el-table-column prop="dimension" label="考察维度" width="150" />
          <el-table-column prop="rationale" label="选择理由" min-width="260" />
        </el-table>
      </div>

      <div v-if="report.status === 'running'" class="alert-inline info">
        <strong>这份报告仍在生成中。</strong>
        题目由评审收尾一次性落库，完成前题目列表为空是正常状态；页面每 2 秒轮询的后台任务仍在跑。
      </div>
      <div v-else-if="report.status === 'aborted'" class="alert-inline warn">
        <strong>后台任务没跑到收尾。</strong>
        上游报错或成本护栏触发，库里因此没有题目行；批注只能说明哪些题该改，不能解释为什么没有题。
      </div>

      <ReportViewer :report="report" />
    </template>
  </div>
</template>

<style scoped>
.report-headline {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(280px, 1fr);
  gap: 24px;
  align-items: start;
}

.contact-card {
  padding: 6px 14px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 18px;
}

.meta-grid code {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-2);
}

.plan-strategy {
  padding: 12px 14px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-2);
  white-space: pre-wrap;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-left: 3px solid color-mix(in srgb, var(--brand) 55%, transparent);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

@media (max-width: 1080px) {
  .report-headline,
  .meta-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .meta-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
