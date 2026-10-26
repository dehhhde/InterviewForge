<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { createJob, updateJob } from '@/api'
import { isApiFailure, writeFailureHint } from '@/api/http'
import type { Job, JobPayload } from '@/api/types'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    job?: Job | null
    draft?: JobPayload | null
    skillOptions?: string[]
  }>(),
  { job: null, draft: null, skillOptions: () => [] },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [job: Job]
  'edit-existing': [jobId: string]
}>()

const formRef = ref<FormInstance>()
const titleInput = ref<{ focus: () => void } | null>(null)
const saving = ref(false)
const conflict = ref<{ message: string; jobId: string } | null>(null)

const form = reactive({
  title: '',
  company: '',
  min_years: 0,
  jd_text: '',
  required_skills: [] as string[],
})

const rules: FormRules = {
  title: [
    { required: true, message: '岗位名称必填', trigger: 'blur' },
    { max: 200, message: '岗位名称最多 200 字', trigger: 'blur' },
  ],
  company: [{ max: 200, message: '公司名称最多 200 字', trigger: 'blur' }],
  jd_text: [
    { required: true, message: 'JD 正文必填', trigger: 'blur' },
    { min: 5, message: 'JD 正文至少 5 个字，否则出题没有依据', trigger: 'blur' },
  ],
}

const isEdit = computed(() => Boolean(props.job?.id))
const title = computed(() => {
  if (isEdit.value) return `编辑岗位 · ${props.job?.title ?? ''}`
  return props.draft ? '确认图片识别草稿' : '新建岗位'
})

function reset(): void {
  conflict.value = null
  const source = props.job ?? props.draft ?? null
  form.title = source?.title ?? ''
  form.company = source?.company ?? ''
  form.min_years = source?.min_years ?? 0
  form.jd_text = source?.jd_text ?? ''
  form.required_skills = [...(source?.required_skills ?? [])]
  void nextTick(() => {
    formRef.value?.clearValidate()
    if (!form.title) titleInput.value?.focus()
  })
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  },
)

watch(
  () => props.job,
  () => {
    if (props.modelValue) reset()
  },
)

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  const payload: JobPayload = {
    title: form.title.trim(),
    company: form.company.trim(),
    min_years: Number(form.min_years) || 0,
    jd_text: form.jd_text,
    required_skills: form.required_skills.map((skill) => skill.trim()).filter(Boolean),
  }

  saving.value = true
  conflict.value = null
  try {
    const saved = props.job
      ? await updateJob(props.job.id, payload)
      : await createJob(payload)
    ElMessage.success(
      props.job
        ? `已更新：${saved.title}`
        : `已创建：${saved.title}（派生技能 ${saved.required_skills.length} 项）`,
    )
    emit('saved', saved)
    close()
  } catch (error) {
    if (isApiFailure(error) && error.code === 'already_exists') {
      const jobId = String((error.details?.job_id as string | undefined) ?? '')
      conflict.value = { message: error.message, jobId }
      return
    }
    ElMessage.error(
      writeFailureHint(error, '保存失败', '岗位名 + 公司是自然键，再存一次会撞 409'),
    )
  } finally {
    saving.value = false
  }
}

function editExisting(): void {
  if (conflict.value?.jobId) emit('edit-existing', conflict.value.jobId)
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    :title="title"
    size="580px"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-if="draft && !isEdit" class="alert-inline info" style="margin-bottom: 16px">
      这是一份<strong>未落库</strong>的图片识别草稿。认不出的字段留空，保存时才走
      <code>POST /jobs</code> 真正写入。
    </div>

    <el-alert v-if="conflict" type="warning" :closable="false" show-icon style="margin-bottom: 16px">
      <template #title>岗位名 + 公司已存在</template>
      <p>{{ conflict.message }}</p>
      <el-button v-if="conflict.jobId" size="small" style="margin-top: 8px" @click="editExisting">
        载入已有岗位继续编辑
      </el-button>
    </el-alert>

    <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
      <el-form-item label="岗位名称" prop="title">
        <el-input
          ref="titleInput"
          v-model="form.title"
          maxlength="200"
          show-word-limit
          placeholder="后端工程师"
        />
      </el-form-item>
      <el-form-item label="公司（可留空）" prop="company">
        <el-input v-model="form.company" maxlength="200" placeholder="示例公司" />
      </el-form-item>
      <el-form-item label="期望年限">
        <el-input-number v-model="form.min_years" :min="0" :max="60" :step="0.5" style="width: 100%" />
      </el-form-item>
      <el-form-item label="必备技能（留空则按 JD 自动派生）">
        <el-select
          v-model="form.required_skills"
          multiple
          filterable
          allow-create
          default-first-option
          :reserve-keyword="false"
          placeholder="输入技能后回车，例如 Python / Redis"
        >
          <el-option
            v-for="skill in skillOptions"
            :key="skill"
            :label="skill"
            :value="skill"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="JD 正文" prop="jd_text">
        <el-input
          v-model="form.jd_text"
          type="textarea"
          :rows="11"
          maxlength="20000"
          show-word-limit
          placeholder="岗位职责：&#10;1. 负责高并发 API 网关；&#10;任职要求：&#10;精通 Python，熟悉 Redis"
        />
      </el-form-item>
    </el-form>

    <p class="hint">
      岗位写入是同步的：JD 里的技能由 <code>SKILL_ALIASES</code> 词典当场识别，不花模型调用。
      岗位名 + 公司是自然键，重复提交会返回 409。
    </p>

    <template #footer>
      <div style="display: flex; justify-content: flex-end; gap: 8px">
        <el-button @click="close">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">
          {{ isEdit ? '保存修改' : '创建岗位' }}
        </el-button>
      </div>
    </template>
  </el-drawer>
</template>
