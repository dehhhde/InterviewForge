<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, ArrowUp } from '@element-plus/icons-vue'

import type { InterviewQuestion, ReviewIssue } from '@/api/types'
import DifficultyTag from '@/components/DifficultyTag.vue'
import SourceTag from '@/components/SourceTag.vue'
import { SOURCE_LABEL } from '@/utils/format'

const props = defineProps<{
  question: InterviewQuestion
  index: number
  issues?: ReviewIssue[]
  reportId?: string
}>()

const showEvidence = computed(
  () => props.question.evidence.trim().length > 0 || props.question.source !== 'generic',
)

const draftKey = computed(() =>
  props.reportId ? `if-console-answer:${props.reportId}:${props.index}` : '',
)
const answer = ref('')
const answerOpen = ref(false)
const answerLength = computed(() => answer.value.trim().length)
const answered = computed(() => answerLength.value > 0)

// 同一份报告换题时（筛选不影响 index）重新载入对应草稿
watch(
  draftKey,
  (key) => {
    answer.value = key ? (localStorage.getItem(key) ?? '') : ''
  },
  { immediate: true },
)

watch(answer, (value) => {
  if (!draftKey.value) return
  if (value.trim()) localStorage.setItem(draftKey.value, value)
  else localStorage.removeItem(draftKey.value)
})

function clearAnswer(): void {
  answer.value = ''
}
</script>

<template>
  <article class="question-card" :class="{ flagged: issues?.length }">
    <div class="question-head">
      <span class="qno">第 {{ index + 1 }} 题</span>
      <DifficultyTag :difficulty="question.difficulty" />
      <SourceTag :source="question.source" />
      <span v-if="issues?.length" class="pill warn">{{ issues.length }} 条批注</span>
    </div>

    <p class="question-stem">{{ question.stem }}</p>

    <div class="section-label">考察点</div>
    <ul class="check-list">
      <li v-for="(checkpoint, i) in question.checkpoints" :key="i">{{ checkpoint }}</li>
    </ul>

    <div v-if="showEvidence" class="evidence-box">
      <strong>依据出处 · {{ SOURCE_LABEL[question.source] ?? question.source }}</strong>
      {{ question.evidence || '无逐字依据' }}
    </div>

    <div class="answer-composer">
      <div class="answer-composer-head">
        <span class="section-label" style="margin: 0">你的作答</span>
        <span class="tiny">
          {{ answerLength }} 字<template v-if="answered"> · 草稿已存在本机</template>
        </span>
      </div>
      <el-input
        v-model="answer"
        type="textarea"
        :autosize="{ minRows: 4, maxRows: 12 }"
        resize="vertical"
        placeholder="先自己作答，再展开标准答案对照。草稿只保存在当前浏览器，不会提交到服务端。"
      />
      <div class="answer-composer-foot">
        <el-button v-if="answered" text size="small" @click="clearAnswer">清空作答</el-button>
        <span v-else class="tiny">答案默认收起，避免先看到参考答案。</span>
        <div class="toolbar-spacer" />
        <el-button
          :icon="answerOpen ? ArrowUp : ArrowDown"
          :aria-expanded="answerOpen"
          @click="answerOpen = !answerOpen"
        >
          {{ answerOpen ? '收起标准答案' : '查看标准答案' }}
        </el-button>
      </div>
    </div>

    <Transition name="answer-reveal">
      <div v-show="answerOpen" class="answer-reveal">
        <div class="section-label">标准答案</div>
        <div class="answer-box">{{ question.reference_answer }}</div>

        <template v-if="question.rubric.length">
          <div class="section-label">打分标准</div>
          <ul class="check-list">
            <li v-for="(rubric, i) in question.rubric" :key="i">{{ rubric }}</li>
          </ul>
        </template>

        <template v-if="question.follow_ups.length">
          <div class="section-label">追问</div>
          <ol class="check-list">
            <li v-for="(followUp, i) in question.follow_ups" :key="i">{{ followUp }}</li>
          </ol>
        </template>
      </div>
    </Transition>

    <div v-if="issues?.length" class="issue-box">
      <strong>评审批注（只标注问题，不拦截交付）</strong>
      <ul>
        <li v-for="(issue, i) in issues" :key="i">
          {{ issue.problem }}<template v-if="issue.fix"> · 建议：{{ issue.fix }}</template>
        </li>
      </ul>
    </div>
  </article>
</template>

<style scoped>
.answer-composer {
  padding: 14px;
  margin-top: 16px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}

.answer-composer-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.answer-composer :deep(.el-textarea__inner) {
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  background: var(--surface);
}

.answer-composer-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.answer-reveal-enter-active,
.answer-reveal-leave-active {
  transition:
    opacity 0.18s ease,
    transform 0.18s ease;
}

.answer-reveal-enter-from,
.answer-reveal-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* 打印时保留原有语义：答案照常出现在纸上，作答框不占位置 */
@media print {
  .answer-reveal {
    display: block !important;
  }

  .answer-composer {
    display: none;
  }
}
</style>
