<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Download,
  Document,
  Refresh,
  Search,
  View,
} from '@element-plus/icons-vue'

import { getInterview, listInterviews } from '@/api'
import type { Interview } from '@/api/types'
import EmptyBlock from '@/components/EmptyBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusPill from '@/components/StatusPill.vue'
import { DIFFICULTY_LABEL, formatDateTime, reportPairLabel, shortId } from '@/utils/format'
import { downloadMarkdown } from '@/utils/markdown'

const router = useRouter()

type StatusFilter = 'all' | 'running' | 'completed' | 'aborted'

const STATUS_OPTIONS: Array<{ key: StatusFilter; label: string; tone: string }> = [
  { key: 'all', label: '全部报告', tone: '' },
  { key: 'running', label: '进行中', tone: 'live' },
  { key: 'completed', label: '已完成', tone: 'ok' },
  { key: 'aborted', label: '已中止', tone: 'warn' },
]

const rows = ref<Interview[]>([])
const loading = ref(true)
const keyword = ref('')
const status = ref<StatusFilter>('all')
const page = ref(1)
const pageSize = ref(10)
const exportingId = ref('')

let timer: number | null = null

const counts = computed(() => {
  const result = { all: rows.value.length, running: 0, completed: 0, aborted: 0 }
  for (const report of rows.value) result[report.status] += 1
  return result
})

const filtered = computed(() => {
  const term = keyword.value.trim().toLowerCase()
  return rows.value.filter((report) => {
    if (status.value !== 'all' && report.status !== status.value) return false
    if (!term) return true
    return [
      reportPairLabel(report),
      report.id,
      report.resume_file_name,
      report.job_company,
    ]
      .join(' ')
      .toLowerCase()
      .includes(term)
  })
})

const paged = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filtered.value.slice(start, start + pageSize.value)
})

const emptyCopy = computed(() =>
  rows.value.length
    ? {
        title: '没有匹配的报告',
        description: '换一个关键词，或把状态筛选切回「全部报告」。',
      }
    : {
        title: '还没有报告',
        description: '报告由七节点工作流产出，完成一次出题后就会出现在这里。',
      },
)

const hasRunning = computed(() => rows.value.some((report) => report.status === 'running'))

watch([keyword, status], () => {
  page.value = 1
})

watch(pageSize, () => {
  page.value = 1
})

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    rows.value = await listInterviews(200)
    const lastPage = Math.max(1, Math.ceil(filtered.value.length / pageSize.value))
    if (page.value > lastPage) page.value = lastPage
  } catch (error) {
    if (!silent) ElMessage.error((error as Error).message)
  } finally {
    if (!silent) loading.value = false
  }
}

function openReport(report: Interview): void {
  void router.push(`/reports/${report.id}`)
}

async function exportReport(report: Interview): Promise<void> {
  exportingId.value = report.id
  try {
    // 列表接口不带题目行，导出必须再取一次详情，别拿列表里的空数组生成一份空卷。
    const full = await getInterview(report.id)
    downloadMarkdown(full)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    exportingId.value = ''
  }
}

function planQuotas(report: Interview): Array<{ label: string; value: number }> {
  const quotas = report.plan?.difficulty_quotas ?? {}
  return Object.entries(quotas)
    .filter(([, value]) => typeof value === 'number')
    .map(([key, value]) => ({ label: DIFFICULTY_LABEL[key] ?? key, value: value as number }))
}

function planItems(report: Interview): NonNullable<Interview['plan']['items']> {
  return report.plan?.items ?? []
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => {
    if (hasRunning.value) void load(true)
  }, 8000)
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <div class="page">
    <PageHeader
      eyebrow="REPORTS"
      title="报告中心"
      subtitle="报告头保存生成那一刻的简历与岗位快照：简历改名、岗位下线都不会改写历史。批注只标出该改的题，不影响卷子的交付。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="load()">刷新</el-button>
        <el-button type="primary" :icon="Document" @click="router.push('/generate')">
          发起出题
        </el-button>
      </template>
    </PageHeader>

    <div class="status-strip">
      <button
        v-for="item in STATUS_OPTIONS"
        :key="item.key"
        class="status-chip"
        :class="{ active: status === item.key }"
        @click="status = item.key"
      >
        <span>{{ item.label }}</span>
        <b>{{ counts[item.key] }}</b>
        <i v-if="item.tone" class="status-chip-dot" :class="item.tone" />
      </button>
    </div>

    <div class="card flush">
      <div class="card-head" style="padding: 20px 22px 0; margin-bottom: 12px">
        <div>
          <h3>历史报告 <span class="tiny">共 {{ filtered.length }} 份</span></h3>
          <p>运行中的报告每 8 秒静默刷新一次；点一行进入详情，可筛选题目并导出 Markdown。</p>
        </div>
        <el-input
          v-model="keyword"
          placeholder="搜索简历 / 岗位 / 公司 / 报告 id"
          clearable
          style="width: 300px"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <el-table
        :data="paged"
        v-loading="loading"
        row-class-name="clickable-row"
        @row-click="openReport"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand-grid">
              <div>
                <span class="section-label" style="margin-top: 0">出题策略</span>
                <p class="tiny">{{ row.plan?.strategy || '本次报告没有记录策略说明。' }}</p>
                <div class="chip-row" style="margin-top: 8px">
                  <span v-for="quota in planQuotas(row)" :key="quota.label" class="skill-chip">
                    {{ quota.label }} <b>{{ quota.value }}</b>
                  </span>
                  <span v-if="!planQuotas(row).length" class="tiny">无难度配额记录</span>
                </div>
              </div>
              <div>
                <span class="section-label" style="margin-top: 0">评审批注</span>
                <ul v-if="row.review_issues.length" class="issue-list">
                  <li v-for="(issue, index) in row.review_issues.slice(0, 6)" :key="index">
                    <b>
                      {{ issue.index === null || issue.index === undefined ? '整批' : `第 ${issue.index + 1} 题` }}
                    </b>
                    {{ issue.problem }}
                  </li>
                </ul>
                <p v-else class="tiny">没有批注：规则层与模型层都没挑出该改的题。</p>
                <p v-if="row.review_issues.length > 6" class="tiny" style="margin-top: 6px">
                  另有 {{ row.review_issues.length - 6 }} 条，进入详情查看。
                </p>
              </div>
              <div>
                <span class="section-label" style="margin-top: 0">计划槽位</span>
                <p v-if="!planItems(row).length" class="tiny">没有可展示的槽位明细。</p>
                <ul v-else class="issue-list">
                  <li v-for="(item, index) in planItems(row).slice(0, 6)" :key="index">
                    <b>{{ item.topic || '未命名主题' }}</b>
                    {{ item.difficulty ? `· ${DIFFICULTY_LABEL[item.difficulty] ?? item.difficulty}` : '' }}
                    {{ item.count ? `· ${item.count} 题` : '' }}
                  </li>
                </ul>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="简历 × 岗位" min-width="320">
          <template #default="{ row }">
            <div class="table-title">{{ reportPairLabel(row) }}</div>
            <div class="table-sub">
              {{ shortId(row.id, 12) }} · 生成于 {{ formatDateTime(row.created_at) }}
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
              {{ row.review_issues.length }} 条
            </span>
            <span v-else class="tiny">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button text size="small" :icon="View" @click.stop="openReport(row)">
                查看
              </el-button>
              <el-button
                text
                size="small"
                :icon="Download"
                :loading="exportingId === row.id"
                @click.stop="exportReport(row)"
              >
                导出
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyBlock :title="emptyCopy.title" :description="emptyCopy.description">
            <el-button
              v-if="!rows.length"
              type="primary"
              size="small"
              :icon="Document"
              @click="router.push('/generate')"
            >
              发起出题
            </el-button>
          </EmptyBlock>
        </template>
      </el-table>

      <div style="padding: 0 22px 18px">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="filtered.length"
          layout="total, sizes, prev, pager, next"
          background
        />
      </div>
    </div>

    <div class="alert-inline info">
      <strong>「已中止」只有一条来路：</strong>后台任务没跑到收尾（上游报错或成本护栏触发）。
      它不再代表"质量门把整批判死"——批注从 ADR-0015 起只标出该改的题，题目一律照常落库。
    </div>
  </div>
</template>

<style scoped>
.status-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.status-chip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 9px 16px;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow-sm);
  transition: border-color 0.15s ease, color 0.15s ease, transform 0.15s ease;
}

.status-chip:hover {
  transform: translateY(-1px);
  border-color: var(--line-strong);
}

.status-chip.active {
  color: var(--brand);
  background: var(--brand-soft);
  border-color: color-mix(in srgb, var(--brand) 36%, transparent);
}

.status-chip b {
  font-size: 15px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.status-chip-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.status-chip-dot.live {
  background: var(--live);
}

.status-chip-dot.ok {
  background: var(--ok);
}

.status-chip-dot.warn {
  background: var(--warn);
}

.expand-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 22px;
  padding: 6px 22px 16px 56px;
}

.issue-list {
  padding-left: 16px;
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-2);
}

.issue-list b {
  font-weight: 620;
  color: var(--text);
}

@media (max-width: 1080px) {
  .expand-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 14px;
    padding-left: 24px;
  }
}
</style>
