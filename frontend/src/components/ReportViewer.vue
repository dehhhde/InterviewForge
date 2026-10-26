<script setup lang="ts">
import { computed, ref } from 'vue'
import { Document } from '@element-plus/icons-vue'

import type { Interview } from '@/api/types'
import EmptyBlock from '@/components/EmptyBlock.vue'
import QuestionCard from '@/components/QuestionCard.vue'
import { DIFFICULTY_LABEL, ORIGIN_LABEL, SOURCE_LABEL, issuesByIndex } from '@/utils/format'

const props = defineProps<{ report: Interview }>()

const difficultyFilter = ref<'all' | 'easy' | 'medium' | 'hard'>('all')
const sourceFilter = ref<'all' | 'resume' | 'jd' | 'generic'>('all')
const keyword = ref('')

const flagged = computed(() => issuesByIndex(props.report.review_issues))

const filtered = computed(() =>
  props.report.questions
    .map((question, index) => ({ question, index }))
    .filter(({ question }) => {
      if (difficultyFilter.value !== 'all' && question.difficulty !== difficultyFilter.value) {
        return false
      }
      if (sourceFilter.value !== 'all' && question.source !== sourceFilter.value) return false
      const term = keyword.value.trim().toLowerCase()
      if (!term) return true
      const haystack = [
        question.stem,
        question.reference_answer,
        question.checkpoints.join(' '),
        question.evidence,
      ]
        .join(' ')
        .toLowerCase()
      return haystack.includes(term)
    }),
)

const difficultyCounts = computed(() => {
  const counts = { easy: 0, medium: 0, hard: 0 }
  for (const question of props.report.questions) counts[question.difficulty] += 1
  return counts
})

const sourceCounts = computed(() => {
  const counts = { resume: 0, jd: 0, generic: 0 }
  for (const question of props.report.questions) counts[question.source] += 1
  return counts
})

const emptyTitle = computed(() => {
  if (props.report.status === 'running') return '报告仍在生成中'
  if (props.report.status === 'completed') return '本轮没有产出题目'
  return '后台任务未能跑到收尾'
})

const emptyNote = computed(() => {
  if (props.report.status === 'running') {
    return '题目由评审收尾一次性落库，完成前这里为空是正常状态。'
  }
  if (props.report.status === 'completed') {
    return '收尾拿到了空批次：出题环节这一轮没有产出任何题，请看下方的批注与任务错误。'
  }
  return '上游报错或成本护栏触发，库里因此没有题目行。批注只能说明哪些题该改，不能解释为什么没有题。'
})
</script>

<template>
  <div class="report-viewer">
    <div v-if="!report.questions.length" class="card">
      <EmptyBlock :icon="Document" :title="emptyTitle" :description="emptyNote" />
    </div>

    <template v-else>
      <div class="summary-strip">
        <div class="summary-tile">
          <span>交付题目</span>
          <strong>{{ report.questions.length }}</strong>
        </div>
        <div class="summary-tile">
          <span>难度分布 · 简单 / 中等 / 较难</span>
          <strong>
            {{ difficultyCounts.easy }} / {{ difficultyCounts.medium }} /
            {{ difficultyCounts.hard }}
          </strong>
        </div>
        <div class="summary-tile">
          <span>来源分布 · 简历 / JD / 通用</span>
          <strong>
            {{ sourceCounts.resume }} / {{ sourceCounts.jd }} / {{ sourceCounts.generic }}
          </strong>
        </div>
        <div class="summary-tile">
          <span>评审批注</span>
          <strong>{{ report.review_issues.length }}</strong>
        </div>
      </div>

      <div class="card">
        <div class="toolbar">
          <el-input
            v-model="keyword"
            placeholder="搜索题干、答案、考察点或依据"
            clearable
            style="width: 280px"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-select v-model="difficultyFilter" style="width: 130px">
            <el-option label="全部难度" value="all" />
            <el-option v-for="(label, key) in DIFFICULTY_LABEL" :key="key" :label="label" :value="key" />
          </el-select>
          <el-select v-model="sourceFilter" style="width: 130px">
            <el-option label="全部来源" value="all" />
            <el-option v-for="(label, key) in SOURCE_LABEL" :key="key" :label="label" :value="key" />
          </el-select>
          <div class="toolbar-spacer" />
          <span class="tiny">显示 {{ filtered.length }} / {{ report.questions.length }} 题</span>
        </div>
      </div>

      <div class="question-list">
        <QuestionCard
          v-for="row in filtered"
          :key="`${report.id}-${row.index}`"
          :question="row.question"
          :index="row.index"
          :issues="flagged.get(row.index)"
          :report-id="report.id"
        />
        <div v-if="!filtered.length" class="card">
          <EmptyBlock title="没有匹配的题目" description="换一个关键词或放宽筛选条件。" />
        </div>
      </div>
    </template>

    <div v-if="report.review_issues.length" class="card">
      <div class="card-head">
        <div>
          <h3>评审批注</h3>
          <p>规则层与模型层只标注该改的题，不影响这份卷子的交付。</p>
        </div>
        <span class="pill warn">{{ report.review_issues.length }} 条</span>
      </div>
      <el-table :data="report.review_issues" size="small">
        <el-table-column label="题号" width="90">
          <template #default="{ row }">
            {{ row.index === null || row.index === undefined ? '整批' : `第 ${row.index + 1} 题` }}
          </template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <span class="source-tag">{{ ORIGIN_LABEL[row.origin] ?? row.origin }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="problem" label="问题" min-width="240" />
        <el-table-column prop="fix" label="建议" min-width="220" />
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.report-viewer {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.question-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
