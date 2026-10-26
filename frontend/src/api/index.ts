import { del, get, post, put } from '@/api/http'
import type {
  GenerateReceipt,
  Health,
  Interview,
  Job,
  JobDraft,
  JobPayload,
  Resume,
  Task,
  UploadReceipt,
} from '@/api/types'

export function fetchHealth(): Promise<Health> {
  return get<Health>('/health', { timeout: 10_000 })
}

// --- 简历 ---

export function listResumes(limit = 100): Promise<Resume[]> {
  return get<Resume[]>('/resumes/list', { params: { limit } })
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

export function listJobs(limit = 100): Promise<Job[]> {
  return get<Job[]>('/jobs', { params: { limit } })
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

export function listInterviews(limit = 100): Promise<Interview[]> {
  return get<Interview[]>('/interviews/list', { params: { limit } })
}

export function getInterview(id: string): Promise<Interview> {
  return get<Interview>(`/interviews/${id}`)
}

export function startGeneration(resumeId: string, jobId: string): Promise<GenerateReceipt> {
  return post<GenerateReceipt>('/interviews/generate', {
    resume_id: resumeId,
    job_id: jobId,
  })
}

export function getTask(taskId: string): Promise<Task> {
  return get<Task>(`/tasks/${taskId}`, { timeout: 15_000 })
}
