<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ElMessage,
  ElMessageBox,
  type UploadFile,
  type UploadInstance,
  type UploadUserFile,
} from 'element-plus'
import { Delete, EditPen, Refresh, Search, UploadFilled } from '@element-plus/icons-vue'

import {
  deleteResume as removeResumeApi,
  getResume,
  listResumes,
  renameResume,
  uploadResume,
} from '@/api'
import { isApiFailure, writeFailureHint } from '@/api/http'
import type { Resume, Task } from '@/api/types'
import EmptyBlock from '@/components/EmptyBlock.vue'
import PageHeader from '@/components/PageHeader.vue'
import ResumeProfile from '@/components/ResumeProfile.vue'
import TaskTimeline from '@/components/TaskTimeline.vue'
import { createTaskPoller } from '@/utils/task'
import { formatDateTime, initials, resumeLabel, shortId } from '@/utils/format'

const rows = ref<Resume[]>([])
const loading = ref(true)
const keyword = ref('')

const uploadRef = ref<UploadInstance>()
const uploadFiles = ref<UploadUserFile[]>([])
const uploadFile = ref<File | null>(null)
const uploading = ref(false)
const uploadTask = ref<Task | null>(null)
const uploadStartedAt = ref<number | null>(null)
const uploadFinishedAt = ref<number | null>(null)
const uploadHint = ref('')

const detailOpen = ref(false)
const detail = ref<Resume | null>(null)
const detailLoading = ref(false)

const renameOpen = ref(false)
const renameTarget = ref<Resume | null>(null)
const renameName = ref('')
const renaming = ref(false)

const deletingId = ref('')

const uploadPoller = createTaskPoller()

const filtered = computed(() => {
  const term = keyword.value.trim().toLowerCase()
  if (!term) return rows.value
  return rows.value.filter((resume) => {
    const haystack = [resumeLabel(resume), resume.file_name, resume.parsed.name ?? '']
      .join(' ')
      .toLowerCase()
    return haystack.includes(term)
  })
})

async function load(): Promise<void> {
  loading.value = true
  try {
    rows.value = await listResumes(200)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function onFileChange(file: UploadFile): void {
  uploadFile.value = file.raw ?? null
  uploadHint.value = ''
}

function onFileRemove(): void {
  uploadFile.value = null
}

function onExceed(): void {
  ElMessage.warning('一次只上传一份简历，先移除已选文件')
}

async function startUpload(): Promise<void> {
  if (!uploadFile.value) {
    ElMessage.warning('先选一个 PDF / DOCX 文件')
    return
  }

  uploading.value = true
  uploadTask.value = null
  uploadHint.value = '上传中…'
  uploadStartedAt.value = Date.now()
  uploadFinishedAt.value = null

  try {
    const accepted = await uploadResume(uploadFile.value)
    uploadHint.value = `已受理：resume ${shortId(accepted.resume_id)} / task ${shortId(accepted.task_id)}`
    uploadRef.value?.clearFiles()
    uploadFile.value = null

    const task = await uploadPoller.run(accepted.task_id, (current) => {
      uploadTask.value = current
      if (current.status === 'completed' || current.status === 'failed') {
        uploadFinishedAt.value = Date.now()
      }
    })

    if (!task) {
      uploadHint.value = '已受理，但读不到解析进度——刷新列表确认，别急着重传'
      return
    }
    if (task.status === 'failed') {
      uploadHint.value = '解析失败，详见下方任务错误'
      ElMessage.error('简历解析失败')
      return
    }

    uploadHint.value = '解析完成'
    ElMessage.success('简历解析完成')
    await load()
    await openDetail(accepted.resume_id)
  } catch (error) {
    uploadHint.value = writeFailureHint(error, '上传失败', '再传一次就是多一份简历')
    ElMessage.error((error as Error).message)
  } finally {
    uploading.value = false
    if (!uploadFinishedAt.value) uploadFinishedAt.value = Date.now()
  }
}

async function openDetail(id: string): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  try {
    detail.value = await getResume(id)
  } catch (error) {
    detailOpen.value = false
    ElMessage.error((error as Error).message)
  } finally {
    detailLoading.value = false
  }
}

function openRename(resume: Resume): void {
  renameTarget.value = resume
  renameName.value = resumeLabel(resume)
  renameOpen.value = true
}

async function submitRename(): Promise<void> {
  const name = renameName.value.trim()
  if (!renameTarget.value || !name) {
    ElMessage.warning('展示名不能为空')
    return
  }
  renaming.value = true
  try {
    const updated = await renameResume(renameTarget.value.id, name)
    ElMessage.success('展示名已更新')
    renameOpen.value = false
    if (detail.value?.id === updated.id) detail.value = updated
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    renaming.value = false
  }
}

async function removeResume(resume: Resume): Promise<void> {
  const label = resumeLabel(resume)
  try {
    await ElMessageBox.confirm(
      `删除「${label}」？它名下的报告也会一起消失（库里没有回收站）。`,
      '删除简历',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  deletingId.value = resume.id
  try {
    try {
      await removeResumeApi(resume.id)
    } catch (error) {
      // 409 是服务端在"名下还有报告"时的刹车；连报告一起删必须由人再说一次。
      if (!isApiFailure(error) || error.code !== 'already_exists') throw error
      const count = Number(error.details?.interview_count ?? 0)
      try {
        await ElMessageBox.confirm(
          `这份简历名下有 ${count} 份报告，连报告一起删除？该动作不可撤销。`,
          '确认级联删除',
          { type: 'error', confirmButtonText: '连报告一起删除', cancelButtonText: '取消' },
        )
      } catch {
        return
      }
      await removeResumeApi(resume.id, true)
    }
    ElMessage.success('简历已删除')
    detailOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    deletingId.value = ''
  }
}

onMounted(() => {
  void load()
})

onBeforeUnmount(() => {
  uploadPoller.stop()
})
</script>

<template>
  <div class="page">
    <PageHeader
      eyebrow="RESUMES"
      title="简历库"
      subtitle="上传后立即返回 202，解析在后台协程中运行。抽取结果与原文是证据链的真相源，页面只开放展示名、不开放改写。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <div class="card">
      <div class="card-head">
        <div>
          <h3>上传并解析</h3>
          <p>支持 PDF / DOCX。上传立即返回两个 id，解析完成后自动刷新列表。</p>
        </div>
        <span class="pill muted">POST /resumes/upload · 202</span>
      </div>

      <div class="upload-grid">
        <el-upload
          ref="uploadRef"
          v-model:file-list="uploadFiles"
          drag
          :auto-upload="false"
          :limit="1"
          accept=".pdf,.docx,.doc"
          :on-change="onFileChange"
          :on-remove="onFileRemove"
          :on-exceed="onExceed"
        >
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">把简历拖到这里，或 <em>点击选择</em></div>
          <template #tip>
            <div class="el-upload__tip">一次一份，整份读入内存后落盘（简历是页级小文件）。</div>
          </template>
        </el-upload>

        <div class="upload-progress">
          <template v-if="uploadTask">
            <TaskTimeline
              :steps="uploadTask.progress.steps ?? []"
              :status="uploadTask.status"
              :started-at="uploadStartedAt"
              :finished-at="uploadFinishedAt"
            />
            <div v-if="uploadTask.status === 'failed'" class="alert-inline bad" style="margin-top: 12px">
              {{ uploadTask.error.code }}：{{ uploadTask.error.message }}
            </div>
          </template>
          <EmptyBlock
            v-else
            title="等待上传"
            description="解析进度会显示在这里：parse 节点跑完后回写 raw_text / parsed / name。"
          />
        </div>
      </div>

      <div class="toolbar" style="margin-top: 14px">
        <el-button
          type="primary"
          :icon="UploadFilled"
          :loading="uploading"
          :disabled="!uploadFile"
          @click="startUpload"
        >
          上传并解析
        </el-button>
        <span class="hint">{{ uploadHint || '选择文件后开始上传。' }}</span>
      </div>
    </div>

    <div class="card flush">
      <div class="card-head" style="padding: 20px 22px 0; margin-bottom: 12px">
        <div>
          <h3>简历列表 <span class="tiny">共 {{ rows.length }} 份</span></h3>
          <p>点一行查看抽取画像；改名只动展示名；删除按报告数量决定是否需要二次授权。</p>
        </div>
        <el-input
          v-model="keyword"
          placeholder="搜索姓名 / 文件名"
          clearable
          style="width: 240px"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <el-table :data="filtered" v-loading="loading" row-class-name="clickable-row" @row-click="openDetail($event.id)">
        <el-table-column label="简历" min-width="280">
          <template #default="{ row }">
            <div class="resume-cell">
              <span class="resume-avatar">{{ initials(resumeLabel(row)) }}</span>
              <div>
                <div class="table-title">{{ resumeLabel(row) }}</div>
                <div class="table-sub">{{ row.file_name }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="抽取概览" min-width="240">
          <template #default="{ row }">
            <div class="chip-row">
              <span class="skill-chip"><b>{{ row.parsed.skills?.length ?? 0 }}</b> 技能</span>
              <span class="skill-chip">
                <b>{{ row.parsed.years_experience ?? '—' }}</b> 年经验
              </span>
              <span class="skill-chip">
                <b>{{ row.parsed.work_experiences?.length ?? 0 }}</b> 段工作
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">
            <span class="tiny">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button text size="small" :icon="EditPen" @click.stop="openRename(row)">
                改名
              </el-button>
              <el-button
                text
                size="small"
                type="danger"
                :icon="Delete"
                :loading="deletingId === row.id"
                @click.stop="removeResume(row)"
              >
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyBlock
            :title="rows.length ? '没有匹配的简历' : '还没有简历'"
            :description="
              rows.length
                ? '换一个姓名或文件名关键词。'
                : '拖一份 PDF 或 DOCX 到上面的上传区，解析完成后就能在这里看到结构化画像。'
            "
          />
        </template>
      </el-table>
    </div>

    <el-drawer
      v-model="detailOpen"
      size="720px"
      :title="detail ? resumeLabel(detail) : '简历详情'"
    >
      <div v-if="detailLoading" v-loading="true" style="height: 240px" />
      <template v-else-if="detail">
        <div class="alert-inline info" style="margin-bottom: 16px">
          只读视图：<code>raw_text</code> 与 <code>parsed</code> 是抽取与证据核对的事实来源，
          不开放编辑入口。需要重抽请重新上传。
        </div>
        <ResumeProfile :parsed="detail.parsed" />
        <div class="divider" />
        <div class="kv-row">
          <span>简历 id</span>
          <span class="mono">{{ detail.id }}</span>
        </div>
        <div class="kv-row">
          <span>文件名</span>
          <span>{{ detail.file_name }}</span>
        </div>
        <div class="kv-row">
          <span>上传时间</span>
          <span>{{ formatDateTime(detail.created_at) }}</span>
        </div>
      </template>
      <template #footer>
        <div v-if="detail" style="display: flex; justify-content: flex-end; gap: 8px">
          <el-button :icon="EditPen" @click="openRename(detail)">改名</el-button>
          <el-button
            type="danger"
            plain
            :icon="Delete"
            :loading="deletingId === detail.id"
            @click="removeResume(detail)"
          >
            删除简历
          </el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="renameOpen" title="修改展示名" width="420px">
      <el-input
        v-model="renameName"
        maxlength="120"
        show-word-limit
        placeholder="例如：张三 · 后端"
        @keyup.enter="submitRename"
      />
      <p class="hint" style="margin-top: 10px">
        只改展示名；抽取结果与原文不动。报告头里的简历名是生成当时的快照，不会跟着改。
      </p>
      <template #footer>
        <el-button @click="renameOpen = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="submitRename">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.upload-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
  align-items: stretch;
}

.upload-progress {
  min-height: 180px;
  padding: 14px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}

.resume-cell {
  display: flex;
  align-items: center;
  gap: 11px;
}

.resume-avatar {
  display: grid;
  flex: 0 0 36px;
  place-items: center;
  width: 36px;
  height: 36px;
  font-size: 14px;
  font-weight: 700;
  color: var(--brand);
  background: var(--brand-soft);
  border-radius: 10px;
}

@media (max-width: 900px) {
  .upload-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
