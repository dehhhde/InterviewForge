<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MagicStick, Refresh, VideoPause } from '@element-plus/icons-vue'

import { getInterview, listJobs, listResumes, startGeneration } from '@/api'
import { writeFailureHint } from '@/api/http'
import type {
  GenerateReceipt,
  Interview,
  JobListItem,
  ResumeListItem,
  Task,
} from '@/api/types'
import EmptyBlock from '@/components/EmptyBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import ReportViewer from '@/components/ReportViewer.vue'
import StatusPill from '@/components/StatusPill.vue'
import TaskTimeline from '@/components/TaskTimeline.vue'
import { downloadReportMarkdown } from '@/utils/download'
import { MAX_INTERVAL_MS, POLL_INTERVAL_MS, createTaskPoller } from '@/utils/task'
import { resumeLabel } from '@/utils/format'

const router = useRouter()

const resumes = ref<ResumeListItem[]>([])
const jobs = ref<JobListItem[]>([])
const resumeId = ref('')
const jobId = ref('')
const loadingOptions = ref(true)

const starting = ref(false)
const autoFix = ref(false)
const task = ref<Task | null>(null)
const receipt = ref<GenerateReceipt | null>(null)
const report = ref<Interview | null>(null)
const startedAt = ref<number | null>(null)
const finishedAt = ref<number | null>(null)
const errorMessage = ref('')
const pollError = ref('')
const stopped = ref(false)
const activeTaskId = ref('')

const poller = createTaskPoller({
  onError: (error) => {
    pollError.value = error.message
  },
})

const selectedResume = computed(
  () => resumes.value.find((resume) => resume.id === resumeId.value) ?? null,
)
const selectedJob = computed(() => jobs.value.find((job) => job.id === jobId.value) ?? null)

/**
 * 可以重新轮询〔T-305〕：读进度失败之后，正确的动作是**接着看同一个任务**，
 * 而不是再点一次「开始生成」——那一点是两份报告、一轮模型开销。
 */
const degradedNote = computed(() => {
  if (!task.value?.degraded) return ''
  if (task.value.degraded_reason === 'report_gone') {
    return '报告行已经被删除（删除简历时勾了「连同报告一起删」），任务行还在，但已经没有可读的卷子。'
  }
  // 不说"再试一次"：重传同一份简历会得到同一批语料，先看批注与运行观测才是有效动作
  return (
    '后台任务跑完了，评审也照常收尾，但这批题目一道都没通过落库前的校验。' +
    '先看报告详情里的批注与运行观测，确认是语料问题还是模型输出被判废；' +
    '直接再点一次「开始生成」只会拿到同一批语料，除非你先改了简历或岗位描述。'
  )
})

const canRetryPolling = computed(
  () => Boolean(activeTaskId.value) && !report.value && Boolean(errorMessage.value),
)

function jobLabel(job: JobListItem): string {
  return job.company ? `${job.title} · ${job.company}` : job.title
}

async function loadOptions(): Promise<void> {
  loadingOptions.value = true
  try {
    const [resumePage, jobPage] = await Promise.all([listResumes(), listJobs()])
    resumes.value = resumePage.items
    jobs.value = jobPage.items
    if (!resumePage.items.some((resume) => resume.id === resumeId.value)) {
      resumeId.value = resumePage.items[0]?.id ?? ''
    }
    if (!jobPage.items.some((job) => job.id === jobId.value)) {
      jobId.value = jobPage.items[0]?.id ?? ''
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loadingOptions.value = false
  }
}

async function start(): Promise<void> {
  if (!resumeId.value || !jobId.value) {
    ElMessage.warning('简历和岗位都要选')
    return
  }

  poller.stop()
  starting.value = true
  task.value = null
  receipt.value = null
  report.value = null
  errorMessage.value = ''
  pollError.value = ''
  stopped.value = false
  startedAt.value = Date.now()
  finishedAt.value = null

  try {
    const accepted = await startGeneration(resumeId.value, jobId.value, autoFix.value)
    receipt.value = accepted
    activeTaskId.value = accepted.task_id
  } catch (error) {
    errorMessage.value = writeFailureHint(
      error,
      (error as Error).message,
      '再生成一次就是两份报告、一轮模型开销',
    )
    starting.value = false
    return
  }

  await follow(activeTaskId.value)
}

/** 轮询一个已经存在的任务：`start()` 拿到回执后走这里，「重新轮询进度」也走这里。 */
async function follow(taskId: string): Promise<void> {
  starting.value = true
  errorMessage.value = ''
  pollError.value = ''
  stopped.value = false
  try {
    const finalTask = await poller.run(taskId, (current) => {
      task.value = current
      pollError.value = ''
      if (current.status === 'completed' || current.status === 'failed') {
        finishedAt.value = Date.now()
      }
    })

    if (!finalTask) {
      errorMessage.value = stopped.value
        ? '已停止轮询。后台图可能仍在运行，稍后到报告中心确认结果。'
        : pollError.value ||
          `连续读不到任务进度（失败之间已退避到 ${MAX_INTERVAL_MS / 1000} 秒上限）。` +
            '后台图可能仍在运行，可点下方「重新轮询进度」，或稍后刷新报告列表确认——不要直接再点一次生成。'
      return
    }

    if (finalTask.status === 'failed') {
      ElMessage.error('后台任务失败，详情见任务回执。')
      return
    }

    if (!receipt.value) return
    const loaded = await getInterview(receipt.value.interview_id)
    report.value = loaded
    if (finalTask.degraded) {
      // 后台跑完了但卷子不可用〔T-309〕：不能说"出题完成"，那会让用户以为只是题少
      ElMessage.warning('出题已结束，但这份卷子没有可用的题目，详情见下方提示。')
      return
    }
    ElMessage.success(`出题完成：${loaded.questions.length} 道题`)
  } catch (error) {
    errorMessage.value = writeFailureHint(
      error,
      (error as Error).message,
      '再取一次详情不花钱，但重新生成会',
    )
  } finally {
    starting.value = false
    if (!finishedAt.value) finishedAt.value = Date.now()
  }
}

function stopPolling(): void {
  stopped.value = true
  poller.stop()
  errorMessage.value = '已停止轮询（后台任务不会被取消）。'
}

onMounted(() => {
  void loadOptions()
})

onBeforeUnmount(() => {
  poller.stop()
})
</script>

<template>
  <div class="page">
    <PageHeader
      eyebrow="GENERATE"
      title="生成面试题"
      subtitle="选择一份已解析简历与一个岗位，后台运行完整七节点工作流。页面只负责轮询进度与展示结果，所有判断留在服务端。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="loadingOptions" @click="loadOptions">刷新选项</el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="!loadingOptions && (!resumes.length || !jobs.length)"
      type="warning"
      :closable="false"
      show-icon
    >
      <template #title>缺少出题输入</template>
      <p>
        <template v-if="!resumes.length">还没有已解析的简历，先到「简历库」上传。</template>
        <template v-if="!jobs.length">还没有岗位，先到「岗位库」新建或导入 JD。</template>
      </p>
      <div class="toolbar" style="margin-top: 8px">
        <el-button v-if="!resumes.length" size="small" @click="router.push('/resumes')">
          去简历库
        </el-button>
        <el-button v-if="!jobs.length" size="small" @click="router.push('/jobs')">
          去岗位库
        </el-button>
      </div>
    </el-alert>

    <div class="split">
      <div class="card">
        <div class="card-head">
          <div>
            <h3>出题配置</h3>
            <p>报告头会保存生成那一刻的简历名与岗位名快照。</p>
          </div>
          <span class="pill muted">POST /interviews/generate</span>
        </div>

        <el-form label-position="top" v-loading="loadingOptions">
          <el-form-item label="简历">
            <el-select
              v-model="resumeId"
              filterable
              aria-label="选择一份已解析简历"
              placeholder="选择一份已解析简历"
            >
              <el-option
                v-for="resume in resumes"
                :key="resume.id"
                :label="resumeLabel(resume)"
                :value="resume.id"
              >
                <div class="option-row">
                  <span>{{ resumeLabel(resume) }}</span>
                  <span class="tiny">{{ resume.overview.skill_count }} 项技能</span>
                </div>
              </el-option>
            </el-select>
          </el-form-item>

          <el-form-item label="岗位">
            <el-select
              v-model="jobId"
              filterable
              aria-label="选择一个岗位"
              placeholder="选择一个岗位"
            >
              <el-option
                v-for="job in jobs"
                :key="job.id"
                :label="jobLabel(job)"
                :value="job.id"
              >
                <div class="option-row">
                  <span>{{ jobLabel(job) }}</span>
                  <span class="tiny">{{ job.required_skills.length }} 项技能</span>
                </div>
              </el-option>
            </el-select>
          </el-form-item>

          <el-form-item label="评审后自动修复">
            <el-switch v-model="autoFix" aria-label="评审点名的档位重出一遍" />
            <p class="switch-hint">
              开：第一轮评审点过名的那些档整档重出一遍，没被点名的题原样保留。
              代价是<strong>最多多一轮出题开销</strong>（约等于多跑一次扇出），且只重做一轮。
              关：一次成卷，批注只标在报告里，不改题目。
            </p>
          </el-form-item>
        </el-form>

        <div v-if="selectedResume" class="selection-card">
          <div class="selection-head">
            <strong>{{ resumeLabel(selectedResume) }}</strong>
            <span class="tiny">
              经验 {{ selectedResume.overview.years_experience ?? '—' }} 年
            </span>
          </div>
          <div class="chip-row">
            <span
              v-for="skill in selectedResume.overview.skill_names"
              :key="skill"
              class="skill-chip"
            >
              {{ skill }}
            </span>
            <span
              v-if="selectedResume.overview.skill_count > selectedResume.overview.skill_names.length"
              class="count-chip"
            >
              +{{ selectedResume.overview.skill_count - selectedResume.overview.skill_names.length }}
            </span>
          </div>
        </div>

        <div v-if="selectedJob" class="selection-card">
          <div class="selection-head">
            <strong>{{ jobLabel(selectedJob) }}</strong>
            <span class="tiny">期望 {{ selectedJob.min_years }} 年</span>
          </div>
          <div class="chip-row">
            <span
              v-for="skill in selectedJob.required_skills.slice(0, 8)"
              :key="skill"
              class="skill-chip"
            >
              {{ skill }}
            </span>
            <span v-if="selectedJob.required_skills.length > 8" class="count-chip">
              +{{ selectedJob.required_skills.length - 8 }}
            </span>
          </div>
        </div>

        <div class="toolbar" style="margin-top: 18px">
          <el-button
            type="primary"
            size="large"
            :icon="MagicStick"
            :loading="starting"
            :disabled="!resumeId || !jobId"
            @click="start"
          >
            开始生成
          </el-button>
          <span class="hint" role="status">
            实测 80 秒上下，期间每 {{ POLL_INTERVAL_MS / 1000 }} 秒轮询一次任务进度；
            连续读不到时按 {{ POLL_INTERVAL_MS / 1000 }}→{{ MAX_INTERVAL_MS / 1000 }} 秒退避，三次失败就停下等手动重试。
          </span>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <h3>任务进度</h3>
            <p>进度来自 <code>task.progress.steps</code>，题目只在评审收尾落库。</p>
          </div>
          <StatusPill v-if="task" :status="task.status" />
        </div>

        <EmptyBlock
          v-if="!task && !starting"
          title="尚未发起"
          description="选择简历与岗位后点击「开始生成」，这里会显示每个 super-step 的耗时与回退轮次。"
        />

        <template v-else>
          <TaskTimeline
            :steps="task?.progress.steps ?? []"
            :status="task?.status ?? 'pending'"
            :started-at="startedAt"
            :finished-at="finishedAt"
          />

          <div v-if="receipt" class="receipt">
            <div class="kv-row">
              <span>interview_id</span>
              <span class="mono">{{ receipt.interview_id }}</span>
            </div>
            <div class="kv-row">
              <span>task_id</span>
              <span class="mono">{{ receipt.task_id }}</span>
            </div>
          </div>

          <div class="toolbar" style="margin-top: 14px">
            <el-button
              v-if="task && (task.status === 'running' || task.status === 'pending')"
              :icon="VideoPause"
              @click="stopPolling"
            >
              停止轮询
            </el-button>
            <el-button
              v-if="canRetryPolling"
              :icon="Refresh"
              :loading="starting"
              @click="follow(activeTaskId)"
            >
              重新轮询进度
            </el-button>
          </div>

          <div v-if="task?.status === 'failed'" class="alert-inline bad" role="alert" style="margin-top: 14px">
            <strong>{{ task.error.code ?? 'task_failed' }}：{{ task.error.message ?? '未知错误' }}</strong>
            <pre class="error-json">{{ JSON.stringify(task.error.details ?? {}, null, 2) }}</pre>
          </div>

          <div
            v-if="degradedNote"
            class="alert-inline warn"
            role="alert"
            style="margin-top: 14px"
          >
            <strong>任务已完成，但这份卷子不可用（{{ task?.degraded_reason ?? 'no_questions' }}）</strong>
            <p>{{ degradedNote }}</p>
          </div>
        </template>

        <el-alert
          v-if="errorMessage"
          type="error"
          role="alert"
          :closable="false"
          show-icon
          style="margin-top: 14px"
        >
          <template #title>{{ errorMessage }}</template>
        </el-alert>
      </div>
    </div>

    <template v-if="report">
      <div class="card">
        <div class="card-head">
          <div>
            <h3>出题完成</h3>
            <p>报告 {{ report.id.slice(0, 12) }} · 共 {{ report.questions.length }} 道题</p>
          </div>
          <div class="card-actions">
            <el-button @click="downloadReportMarkdown(report.id)">导出 Markdown</el-button>
            <el-button type="primary" @click="router.push(`/reports/${report.id}`)">
              打开完整报告
            </el-button>
          </div>
        </div>
        <div class="summary-strip">
          <div class="summary-tile">
            <span>题目</span>
            <strong>{{ report.questions.length }}</strong>
          </div>
          <div class="summary-tile">
            <span>评审批注</span>
            <strong>{{ report.review_issues.length }}</strong>
          </div>
          <div class="summary-tile">
            <span>状态</span>
            <strong style="font-size: 15px">
              <StatusPill :status="report.status" />
            </strong>
          </div>
          <div class="summary-tile">
            <span>报告 id</span>
            <strong class="mono" style="font-size: 13px">{{ report.id.slice(0, 12) }}</strong>
          </div>
        </div>
      </div>

      <ReportViewer :report="report" />
    </template>
  </div>
</template>

<style scoped>
.switch-hint {
  flex-basis: 100%;
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
}

.option-row {
  display: flex;
  justify-content: space-between;
  gap: 18px;
}

.selection-card {
  padding: 12px 14px;
  margin-top: 10px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}

.selection-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.selection-head strong {
  font-size: 13px;
  font-weight: 620;
}

.receipt {
  padding: 4px 12px;
  margin-top: 16px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}

.error-json {
  max-height: 160px;
  padding: 8px;
  margin: 8px 0 0;
  overflow: auto;
  font-size: 11px;
  white-space: pre-wrap;
  background: color-mix(in srgb, var(--bad) 8%, transparent);
  border-radius: 6px;
}
</style>
