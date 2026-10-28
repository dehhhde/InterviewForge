import { del, get, post, put } from '@/api/http'
import type {
  GenerateReceipt,
  Health,
  Interview,
  InterviewListItem,
  Job,
  JobDraft,
  JobListItem,
  JobPayload,
  Page,
  Resume,
  ResumeListItem,
  Task,
  UploadReceipt,
} from '@/api/types'

/**
 * 列表查询参数〔T-303〕。区间与上限由服务端把关（`PageLimit` 1..200、`PageOffset` 0..10000、
 * `KeywordQuery` ≤64 字），这里只负责把默认值补齐——搜索框要跟着设 `maxlength="64"`，
 * 否则超长关键词会变成一个 422 而不是一句"没搜到"。
 */
export interface ListQuery {
  limit?: number
  offset?: number
  q?: string
}

/** 默认一页取满服务端上限：控制台还没做翻页 UI，先保住"一次看全"的现有行为。 */
const DEFAULT_LIST_LIMIT = 200

function listParams(query: ListQuery): Required<ListQuery> {
  return { limit: DEFAULT_LIST_LIMIT, offset: 0, q: '', ...query }
}

export function fetchHealth(): Promise<Health> {
  return get<Health>('/health', { timeout: 10_000 })
}

// --- 简历 ---

export function listResumes(query: ListQuery = {}): Promise<Page<ResumeListItem>> {
  return get<Page<ResumeListItem>>('/resumes/list', { params: listParams(query) })
}

export function getResume(id: string): Promise<Resume> {
  return get<Resume>(`/resumes/${id}`)
}

export function uploadResume(file: File): Promise<UploadReceipt> {
  const body = new FormData()
  body.append('file', file)
  return post<UploadReceipt>('/resumes/upload', body)
}

export function renameResume(id: string, name: string): Promise<Resume> {
  return put<Resume>(`/resumes/${id}`, { name })
}

export function deleteResume(id: string, force = false): Promise<void> {
  return del(`/resumes/${id}${force ? '?force=true' : ''}`)
}

// --- 岗位 ---

export function listJobs(query: ListQuery = {}): Promise<Page<JobListItem>> {
  return get<Page<JobListItem>>('/jobs', { params: listParams(query) })
}

export function getJob(id: string): Promise<Job> {
  return get<Job>(`/jobs/${id}`)
}

export function createJob(payload: JobPayload): Promise<Job> {
  return post<Job>('/jobs', payload)
}

export function updateJob(id: string, payload: Partial<JobPayload>): Promise<Job> {
  return put<Job>(`/jobs/${id}`, payload)
}

export function deleteJob(id: string): Promise<void> {
  return del(`/jobs/${id}`)
}

export function uploadJob(
  file: File,
  fields: { title: string; company: string; minYears: number },
): Promise<Job> {
  const body = new FormData()
  body.append('file', file)
  body.append('title', fields.title)
  body.append('company', fields.company)
  body.append('min_years', String(fields.minYears))
  return post<Job>('/jobs/upload', body)
}

export function inspectJobImage(file: File): Promise<JobDraft> {
  const body = new FormData()
  body.append('file', file)
  return post<JobDraft>('/jobs/inspect-image', body, { timeout: 180_000 })
}

// --- 报告与任务 ---

export function listInterviews(query: ListQuery = {}): Promise<Page<InterviewListItem>> {
  return get<Page<InterviewListItem>>('/interviews/list', { params: listParams(query) })
}

export function getInterview(id: string): Promise<Interview> {
  return get<Interview>(`/interviews/${id}`)
}

/**
 * 发起一次出题。`autoFix` 是定向重做的授权〔T-404〕：开了以后，第一轮评审点过名的档会重出一遍，
 * 代价是**最多多一轮扇出**，所以默认关，由操作人在页面上明确勾选。
 */
export function startGeneration(
  resumeId: string,
  jobId: string,
  autoFix = false,
): Promise<GenerateReceipt> {
  return post<GenerateReceipt>('/interviews/generate', {
    resume_id: resumeId,
    job_id: jobId,
    auto_fix: autoFix,
  })
}

export function getTask(taskId: string): Promise<Task> {
  return get<Task>(`/tasks/${taskId}`, { timeout: 15_000 })
}
