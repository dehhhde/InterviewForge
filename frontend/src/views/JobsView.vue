<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  ElMessage,
  ElMessageBox,
  type UploadFile,
  type UploadInstance,
  type UploadUserFile,
} from 'element-plus'
import {
  Delete,
  EditPen,
  Picture,
  Plus,
  Refresh,
  Search,
  Upload,
} from '@element-plus/icons-vue'

import {
  deleteJob,
  getJob,
  inspectJobImage,
  listJobs,
  uploadJob,
} from '@/api'
import { isApiFailure, writeFailureHint } from '@/api/http'
import type { Job, JobDraft, JobListItem, JobPayload } from '@/api/types'
import EmptyBlock from '@/components/EmptyBlock.vue'
import JobFormDrawer from '@/components/JobFormDrawer.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useDebouncedSearch } from '@/utils/search'
import { shortId } from '@/utils/format'

const COMMON_SKILLS = [
  'Python', 'Java', 'Go', 'TypeScript', 'Vue', 'React', 'FastAPI', 'Django',
  'Spring Boot', 'MySQL', 'PostgreSQL', 'Redis', 'Kafka', 'Docker', 'Kubernetes',
  'Linux', '分布式', '微服务', '高并发',
]

const rows = ref<JobListItem[]>([])
const total = ref(0)
const loading = ref(true)
const keyword = ref('')

const drawerOpen = ref(false)
const editingJob = ref<Job | null>(null)
const draftPayload = ref<JobPayload | null>(null)

const deletingId = ref('')

const importOpen = ref(false)
const importUploadRef = ref<UploadInstance>()
const importFileList = ref<UploadUserFile[]>([])
const importFile = ref<File | null>(null)
const importTitle = ref('')
const importCompany = ref('')
const importMinYears = ref(0)
const importing = ref(false)
const importHint = ref('')

const imageOpen = ref(false)
const imageUploadRef = ref<UploadInstance>()
const imageFileList = ref<UploadUserFile[]>([])
const imageFile = ref<File | null>(null)
const inspecting = ref(false)
const imageDraft = ref<JobDraft | null>(null)
const imageHint = ref('')

const skillOptions = computed(() => {
  const pool = new Set(COMMON_SKILLS)
  for (const job of rows.value) {
    for (const skill of job.required_skills) pool.add(skill)
  }
  return [...pool].sort((left, right) => left.localeCompare(right, 'zh-CN'))
})

async function load(): Promise<void> {
  loading.value = true
  try {
    // 搜索打在服务端：列表行没有 `jd_text`，本地已经无从比对正文。
    const page = await listJobs({ q: keyword.value.trim() })
    rows.value = page.items
    total.value = page.total
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

useDebouncedSearch(keyword, () => void load())

function openCreate(): void {
  editingJob.value = null
  draftPayload.value = null
  drawerOpen.value = true
}

/**
 * 打开编辑抽屉前先拉一次详情〔T-307〕：列表行只带 JD 摘录，
 * 直接把行数据塞进抽屉会让正文框空着，保存一次就把原文抹掉。
 */
async function openEdit(jobId: string): Promise<void> {
  await onEditExisting(jobId)
  if (editingJob.value?.id !== jobId) return
  draftPayload.value = null
  drawerOpen.value = true
}

async function onEditExisting(jobId: string): Promise<void> {
  try {
    editingJob.value = await getJob(jobId)
    draftPayload.value = null
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function onSaved(): Promise<void> {
  editingJob.value = null
  draftPayload.value = null
  await load()
}

async function removeJob(job: JobListItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除「${job.title}${job.company ? ` · ${job.company}` : ''}」？历史报告不会跟着走——报告头里有岗位名快照。`,
      '删除岗位',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  deletingId.value = job.id
  try {
    await deleteJob(job.id)
    if (editingJob.value?.id === job.id) editingJob.value = null
    ElMessage.success('岗位已删除')
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    deletingId.value = ''
  }
}

// --- 文件导入 ---

function openImport(): void {
  importFile.value = null
  importTitle.value = ''
  importCompany.value = ''
  importMinYears.value = 0
  importHint.value = ''
  importFileList.value = []
  importOpen.value = true
}

function onImportFileChange(file: UploadFile): void {
  importFile.value = file.raw ?? null
  importHint.value = ''
}

function onImportExceed(): void {
  ElMessage.warning('一次只选一个文件，先移除已选文件')
}

async function submitImport(): Promise<void> {
  if (!importFile.value) {
    ElMessage.warning('先选择一个 JD 文件')
    return
  }
  const fallbackTitle = importFile.value.name.replace(/\.[^.]+$/, '')
  const title = importTitle.value.trim() || fallbackTitle
  importing.value = true
  try {
    const job = await uploadJob(importFile.value, {
      title,
      company: importCompany.value.trim(),
      minYears: importMinYears.value || 0,
    })
    ElMessage.success(
      `已入库：${job.title}（识别 ${job.required_skills.length} 项技能）`,
    )
    importUploadRef.value?.clearFiles()
    importFile.value = null
    importOpen.value = false
    await load()
  } catch (error) {
    importHint.value = isApiFailure(error) && error.code === 'already_exists'
      ? '这个岗位名与公司已经存在'
      : writeFailureHint(error, '导入失败', '同名同公司会撞 409，那说明其实已经入库了')
    ElMessage.error((error as Error).message)
  } finally {
    importing.value = false
  }
}

// --- 图片识别 ---

function openImage(): void {
  imageFile.value = null
  imageDraft.value = null
  imageHint.value = ''
  imageFileList.value = []
  imageOpen.value = true
}

function onImageFileChange(file: UploadFile): void {
  imageFile.value = file.raw ?? null
  imageHint.value = ''
}

function onImageExceed(): void {
  ElMessage.warning('一次只选一张图片，先移除已选文件')
}

async function inspectImage(): Promise<void> {
  if (!imageFile.value) {
    ElMessage.warning('先选择一张招聘海报或截图')
    return
  }
  inspecting.value = true
  imageDraft.value = null
  imageHint.value = '识别中…图片要读一次模型，可能要等几十秒'
  try {
    imageDraft.value = await inspectJobImage(imageFile.value)
    imageUploadRef.value?.clearFiles()
    const missing = imageDraft.value.missing_required
    imageHint.value = missing.length
      ? `识别完成，但还缺：${missing.join('、')}`
      : '识别完成，字段齐全，请人工确认后再保存'
  } catch (error) {
    imageHint.value = '识别失败'
    ElMessage.error((error as Error).message)
  } finally {
    inspecting.value = false
  }
}

function applyDraft(): void {
  if (!imageDraft.value) return
  draftPayload.value = {
    title: imageDraft.value.title,
    company: imageDraft.value.company,
    jd_text: imageDraft.value.jd_text,
    required_skills: [...imageDraft.value.required_skills],
    min_years: imageDraft.value.min_years,
  }
  editingJob.value = null
  imageOpen.value = false
  drawerOpen.value = true
}

watch(imageOpen, (open) => {
  if (!open) {
    imageFile.value = null
    imageDraft.value = null
    imageHint.value = ''
    imageUploadRef.value?.clearFiles()
  }
})

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="page">
    <PageHeader
      eyebrow="JOBS"
      title="岗位库"
      subtitle="JD 是上传即定稿的输入，写操作全部同步返回，不占后台任务。岗位名 + 公司是自然键，重复提交返 409。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button :icon="Upload" @click="openImport">导入文件</el-button>
        <el-button :icon="Picture" @click="openImage">图片识别</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建岗位</el-button>
      </template>
    </PageHeader>

    <div class="card flush">
      <div class="card-head" style="padding: 20px 22px 0; margin-bottom: 12px">
        <div>
          <h3>岗位列表 <span class="tiny">共 {{ total }} 个</span></h3>
          <p>点一行进入编辑（编辑抽屉读的是完整 JD）；删除不会带走历史报告。</p>
        </div>
        <el-input
          v-model="keyword"
          placeholder="搜索岗位 / 公司"
          aria-label="搜索岗位名称或公司"
          maxlength="64"
          clearable
          style="width: 280px"
          @keyup.enter="load()"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <el-table :data="rows" v-loading="loading" row-class-name="clickable-row" @row-click="openEdit($event.id)">
        <el-table-column label="岗位" min-width="240">
          <template #default="{ row }">
            <div class="table-title">{{ row.title }}</div>
            <div class="table-sub">
              {{ row.company || '未填写公司' }} · {{ shortId(row.id, 10) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="年限" width="90">
          <template #default="{ row }">
            <span class="mono">{{ row.min_years }}</span>
          </template>
        </el-table-column>
        <el-table-column label="必备技能" min-width="260">
          <template #default="{ row }">
            <div class="chip-row">
              <span v-for="skill in row.required_skills.slice(0, 5)" :key="skill" class="skill-chip">
                {{ skill }}
              </span>
              <span v-if="row.required_skills.length > 5" class="count-chip">
                +{{ row.required_skills.length - 5 }}
              </span>
              <span v-if="!row.required_skills.length" class="tiny">未派生</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="JD 摘要" min-width="260">
          <template #default="{ row }">
            <span class="jd-ellipsis">{{ row.jd_excerpt }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button text size="small" :icon="EditPen" @click.stop="openEdit(row.id)">
                编辑
              </el-button>
              <el-button
                text
                size="small"
                type="danger"
                :icon="Delete"
                :loading="deletingId === row.id"
                @click.stop="removeJob(row)"
              >
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyBlock
            :title="keyword.trim() ? '没有匹配的岗位' : '岗位库还是空的'"
            :description="
              keyword.trim()
                ? '换一个岗位名或公司关键词（服务端只比对这两列，不读 JD 正文）。'
                : '新建一个岗位、导入 JD 文件，或让模型从招聘海报识别草稿。'
            "
          >
            <el-button v-if="!keyword.trim()" type="primary" size="small" @click="openCreate">
              新建岗位
            </el-button>
          </EmptyBlock>
        </template>
      </el-table>
    </div>

    <JobFormDrawer
      v-model="drawerOpen"
      :job="editingJob"
      :draft="draftPayload"
      :skill-options="skillOptions"
      @saved="onSaved"
      @edit-existing="onEditExisting"
    />

    <el-dialog v-model="importOpen" title="从文件导入 JD" width="540px">
      <div class="alert-inline info" style="margin-bottom: 16px">
        支持 .txt / .md / .pdf / .docx。文件读取与技能派生都是同步的，一次模型调用都不花。
      </div>
      <el-upload
        ref="importUploadRef"
        v-model:file-list="importFileList"
        :auto-upload="false"
        :limit="1"
        accept=".txt,.md,.pdf,.docx"
        :on-change="onImportFileChange"
        :on-exceed="onImportExceed"
      >
        <el-button :icon="Upload">选择 JD 文件</el-button>
      </el-upload>
      <el-form label-position="top" style="margin-top: 16px">
        <el-form-item label="岗位名称（留空则用文件名）">
          <el-input v-model="importTitle" maxlength="200" placeholder="后端工程师" />
        </el-form-item>
        <el-form-item label="公司（可留空）">
          <el-input v-model="importCompany" maxlength="200" placeholder="示例公司" />
        </el-form-item>
        <el-form-item label="期望年限">
          <el-input-number v-model="importMinYears" :min="0" :max="60" :step="0.5" />
        </el-form-item>
      </el-form>
      <p v-if="importHint" class="hint" style="color: var(--warn)">{{ importHint }}</p>
      <template #footer>
        <el-button @click="importOpen = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">导入并入库</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="imageOpen" title="从图片识别岗位" width="660px">
      <div class="alert-inline info" style="margin-bottom: 16px">
        识别只返回<strong>草稿，不落库</strong>。岗位名是自然键的一半，必须由人确认后走保存。
      </div>
      <el-upload
        ref="imageUploadRef"
        v-model:file-list="imageFileList"
        :auto-upload="false"
        :limit="1"
        accept=".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff"
        :on-change="onImageFileChange"
        :on-exceed="onImageExceed"
      >
        <el-button :icon="Picture">选择招聘海报 / 截图</el-button>
      </el-upload>
      <p class="hint" style="margin-top: 10px">
        {{ imageHint || '图片要读一次模型（至多再补一次字段），可能要等几十秒。' }}
      </p>

      <div v-if="imageDraft" class="draft-panel">
        <div class="kv-row">
          <span>岗位名称</span>
          <span>{{ imageDraft.title || '未识别（必填）' }}</span>
        </div>
        <div class="kv-row">
          <span>公司</span>
          <span>{{ imageDraft.company || '未识别' }}</span>
        </div>
        <div class="kv-row">
          <span>期望年限</span>
          <span>{{ imageDraft.min_years || '不限' }}</span>
        </div>
        <div class="kv-row">
          <span>派生技能</span>
          <span>
            <span v-for="skill in imageDraft.required_skills" :key="skill" class="skill-chip" style="margin-left: 4px">
              {{ skill }}
            </span>
            <span v-if="!imageDraft.required_skills.length" class="tiny">未识别</span>
          </span>
        </div>
        <div
          v-if="imageDraft.missing_required.length"
          class="alert-inline warn"
          role="alert"
          style="margin-top: 12px"
        >
          还缺必填字段：{{ imageDraft.missing_required.join('、') }}
        </div>
        <div v-if="imageDraft.notes.length" class="alert-inline info" style="margin-top: 10px">
          <div v-for="(note, index) in imageDraft.notes" :key="index">{{ note }}</div>
        </div>
        <pre class="jd-preview">{{ imageDraft.jd_text }}</pre>
      </div>

      <template #footer>
        <el-button @click="imageOpen = false">取消</el-button>
        <el-button type="primary" :loading="inspecting" :disabled="!imageFile" @click="inspectImage">
          识别图片
        </el-button>
        <el-button v-if="imageDraft" type="success" @click="applyDraft">应用到表单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.jd-ellipsis {
  display: -webkit-box;
  overflow: hidden;
  font-size: 12.5px;
  color: var(--text-3);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.draft-panel {
  padding: 14px;
  margin-top: 16px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}

.jd-preview {
  max-height: 180px;
  padding: 10px;
  margin: 12px 0 0;
  overflow: auto;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-2);
  white-space: pre-wrap;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 6px;
}
</style>
