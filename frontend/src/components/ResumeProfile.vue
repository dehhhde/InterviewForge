<script setup lang="ts">
import { computed } from 'vue'
import { Link, Phone } from '@element-plus/icons-vue'

import type { ParsedResume } from '@/api/types'
import { DEGREE_LABEL, LEVEL_LABEL, formatDate, initials } from '@/utils/format'

const props = defineProps<{ parsed: ParsedResume }>()

const skills = computed(() => props.parsed.skills ?? [])
const educations = computed(() => props.parsed.educations ?? [])
const works = computed(() => props.parsed.work_experiences ?? [])
const projects = computed(() => props.parsed.projects ?? [])

function workRange(start?: string | null, end?: string | null): string {
  if (!start && !end) return '时间未识别'
  return `${start ? formatDate(start) : '?'} — ${end ? formatDate(end) : '至今'}`
}
</script>

<template>
  <div class="resume-profile">
    <div class="profile-hero">
      <div class="profile-avatar">{{ initials(parsed.name || '?') }}</div>
      <div>
        <h3>{{ parsed.name || '未识别姓名' }}</h3>
        <p>
          经验 {{ parsed.years_experience ?? '—' }} 年 ·
          {{ skills.length }} 项技能 · {{ works.length }} 段工作经历
        </p>
      </div>
      <div class="profile-contact">
        <span v-if="parsed.phone" class="skill-chip">
          <el-icon><Phone /></el-icon>{{ parsed.phone }}
        </span>
        <span v-if="parsed.email" class="skill-chip">
          <el-icon><Link /></el-icon>{{ parsed.email }}
        </span>
      </div>
    </div>

    <div class="section-label">技能</div>
    <div v-if="skills.length" class="chip-row">
      <el-tooltip
        v-for="skill in skills"
        :key="skill.name"
        :content="`原文：${skill.evidence}`"
        placement="top"
      >
        <span class="skill-chip">
          <b>{{ skill.name }}</b>
          <small>{{ skill.level ? LEVEL_LABEL[skill.level] ?? skill.level : '未标程度' }}</small>
          <small v-if="skill.years">{{ skill.years }} 年</small>
        </span>
      </el-tooltip>
    </div>
    <p v-else class="hint">未抽到技能。</p>

    <div class="section-label">教育经历</div>
    <div v-if="educations.length">
      <div v-for="(item, index) in educations" :key="index" class="timeline-item">
        <strong>{{ item.school }}</strong>
        <span>
          {{ DEGREE_LABEL[item.degree] ?? item.degree }}
          <template v-if="item.major"> · {{ item.major }}</template>
          <template v-if="item.start_year">
            · {{ item.start_year }}–{{ item.graduate_year ?? '?' }}
          </template>
        </span>
        <p class="tiny">原文：{{ item.evidence }}</p>
      </div>
    </div>
    <p v-else class="hint">无教育经历。</p>

    <div class="section-label">工作经历</div>
    <div v-if="works.length">
      <div v-for="(item, index) in works" :key="index" class="timeline-item">
        <strong>{{ item.company }}<template v-if="item.role"> · {{ item.role }}</template></strong>
        <span>{{ workRange(item.start, item.end) }}</span>
        <p v-if="item.summary">{{ item.summary }}</p>
        <p class="tiny">原文：{{ item.evidence }}</p>
      </div>
    </div>
    <p v-else class="hint">无工作经历。</p>

    <div class="section-label">项目经历</div>
    <div v-if="projects.length">
      <div v-for="(item, index) in projects" :key="index" class="project-card">
        <strong>{{ item.name }}<template v-if="item.role"> · {{ item.role }}</template></strong>
        <p v-if="item.summary">{{ item.summary }}</p>
        <ul v-if="item.highlights?.length" class="check-list" style="margin-top: 6px">
          <li v-for="(highlight, i) in item.highlights" :key="i">{{ highlight }}</li>
        </ul>
        <p class="tiny" style="margin-top: 6px">原文：{{ item.evidence }}</p>
      </div>
    </div>
    <p v-else class="hint">无项目经历。</p>
  </div>
</template>
