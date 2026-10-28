export type Difficulty = 'easy' | 'medium' | 'hard'
export type QuestionSource = 'resume' | 'jd' | 'generic'
export type InterviewStatus = 'running' | 'completed' | 'aborted'
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed'
export type SkillLevel = 'basic' | 'familiar' | 'proficient'

/**
 * 列表信封〔T-303〕：`total` 是服务端按**同一过滤条件**数出来的总行数，不是本页条数。
 * 前端"拉 200 条再本地过滤"给不出这两个问题的答案——后面还有多少条只有服务端知道。
 */
export interface Page<T> {
  items: T[]
  total: number
}

export interface Health {
  status: string
  app: string
  env: string
}

export interface SkillItem {
  name: string
  level?: SkillLevel | null
  years?: number | null
  evidence: string
}

export interface EducationItem {
  school: string
  degree: string
  major?: string | null
  start_year?: number | null
  graduate_year?: number | null
  evidence: string
}

export interface WorkExperience {
  company: string
  role?: string | null
  start?: string | null
  end?: string | null
  summary?: string | null
  evidence: string
}

export interface ProjectExperience {
  name: string
  role?: string | null
  summary?: string | null
  highlights?: string[]
  evidence: string
}

/** 抽取结果是"当年通过校验的那份 dump"，字段可能比今天的 schema 少，读时一律容错。 */
export interface ParsedResume {
  name?: string
  email?: string | null
  phone?: string | null
  years_experience?: number
  skills?: SkillItem[]
  educations?: EducationItem[]
  work_experiences?: WorkExperience[]
  projects?: ProjectExperience[]
}

export interface Resume {
  id: string
  file_name: string
  name: string
  parsed: ParsedResume
  created_at: string
}

/**
 * 概览由服务端从 `parsed` **现算**〔T-301〕：列表行不再下发整份抽取 JSON。
 * `skill_names` 只给前 8 个——页面本来就只渲染前 8 个。
 */
export interface ResumeOverview {
  skill_count: number
  skill_names: string[]
  years_experience: number | null
  work_experience_count: number
}

/**
 * 列表行从详情类型**派生**，而不是另抄一份：列名在两处各写一遍就是后端刚清掉的那个形状。
 * 少了 `parsed` 意味着点一行看画像要走 `GET /resumes/{id}`（T-307 的另一半），不是漏了字段。
 */
export type ResumeListItem = Omit<Resume, 'parsed'> & { overview: ResumeOverview }

export interface UploadReceipt {
  resume_id: string
  task_id: string
  status: string
}

export interface Job {
  id: string
  title: string
  company: string
  jd_text: string
  required_skills: string[]
  min_years: number
}

/** 列表行给的是 JD 开头 160 字的摘录〔T-301〕；整篇原文只在 `GET /jobs/{id}` 下发。 */
export type JobListItem = Omit<Job, 'jd_text'> & { jd_excerpt: string }

export interface JobDraft {
  title: string
  company: string
  jd_text: string
  required_skills: string[]
  min_years: number
  missing_required: string[]
  notes: string[]
}

export interface JobPayload {
  title: string
  company: string
  jd_text: string
  required_skills: string[]
  min_years: number
}

export interface InterviewQuestion {
  order_index: number
  stem: string
  difficulty: Difficulty
  source: QuestionSource
  evidence: string
  checkpoints: string[]
  reference_answer: string
  rubric: string[]
  follow_ups: string[]
}

export interface ReviewIssue {
  index: number | null
  problem: string
  fix: string
  origin: 'rule' | 'model' | string
}

export interface InterviewPlan {
  total?: number
  difficulty_quotas?: Partial<Record<Difficulty, number>>
  strategy?: string
  topic_count?: number
  items?: Array<{
    topic?: string
    difficulty?: Difficulty
    count?: number
    dimension?: string
    source?: string
    rationale?: string
  }>
}

export interface Interview {
  id: string
  resume_id: string
  job_id: string | null
  resume_name: string
  resume_file_name: string
  job_title: string
  job_company: string
  status: InterviewStatus
  plan: InterviewPlan
  total_questions: number
  review_issues: ReviewIssue[]
  questions: InterviewQuestion[]
  created_at: string
  updated_at: string
}

/**
 * 报告列表行〔T-301〕：**没有** `questions` 这个键，而不是"空数组"。
 * 这样 `aborted` 的"确实没题"与"列表不发正文"不会共用同一个形状，
 * 页面也不必再去猜"这个空数组是真空还是没下发"。
 */
export type InterviewListItem = Omit<Interview, 'questions'>

export interface GenerateReceipt {
  interview_id: string
  task_id: string
  status: string
}

export interface TaskStep {
  node: string
  ms?: number
}

export interface TaskProgress {
  steps?: TaskStep[]
}

export interface TaskError {
  code?: string
  message?: string
  details?: Record<string, unknown>
}

export interface Task {
  id: string
  kind: string
  status: TaskStatus
  ref_id?: string | null
  progress: TaskProgress
  result: Record<string, unknown>
  error: TaskError
  created_at: string
  updated_at: string
  /** 报告生命周期（服务端派生；非出题任务为空） */
  report_status?: string | null
  /** 报告头的题目数（服务端派生；非出题任务为空） */
  question_count?: number | null
  /** 后台跑完但卷子不可用——0 题或报告行已被删 */
  degraded?: boolean
  degraded_reason?: 'no_questions' | 'report_gone' | null
}
