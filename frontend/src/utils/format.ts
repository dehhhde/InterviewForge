import type { Interview, ReviewIssue } from '@/api/types'

export const DIFFICULTY_LABEL: Record<string, string> = {
  easy: '简单',
  medium: '中等',
  hard: '较难',
}

export const SOURCE_LABEL: Record<string, string> = {
  resume: '简历',
  jd: '岗位 JD',
  generic: '通用',
}

export const STATUS_META: Record<
  string,
  { label: string; tone: 'ok' | 'live' | 'muted' | 'warn' | 'bad' }
> = {
  completed: { label: '已完成', tone: 'ok' },
  running: { label: '进行中', tone: 'live' },
  pending: { label: '排队中', tone: 'muted' },
  aborted: { label: '已中止', tone: 'warn' },
  failed: { label: '失败', tone: 'bad' },
}

export const LEVEL_LABEL: Record<string, string> = {
  basic: '了解',
  familiar: '熟悉',
  proficient: '精通',
}

export const DEGREE_LABEL: Record<string, string> = {
  college: '大专',
  bachelor: '本科',
  master: '硕士',
  phd: '博士',
  other: '其他',
}

export const ORIGIN_LABEL: Record<string, string> = {
  rule: '规则层',
  model: '模型层',
}

export const NODE_LABEL: Record<string, string> = {
  parse: '解析简历',
  validate: '抽取校验',
  match: '岗位匹配',
  profile: '能力画像',
  plan: '出题规划',
  generate: '题目分配',
  generate_item: '并行出题',
  review: '评审收尾',
}

export const NODE_DESCRIPTION: Record<string, string> = {
  parse: '读取原文并抽取结构化事实，每个值都锚回原文',
  validate: '核对日期、证据与字段完整性，必要时回退重解析',
  match: '按技能词典比较简历与 JD，给出缺口',
  profile: '汇总强弱项与考察维度，生成候选人画像',
  plan: '按难度配比与弱项优先，分配出题槽位',
  generate: '按槽位扇出并行出题',
  generate_item: '单个槽位产题，并校验引用出处',
  review: '规则层 + 模型层评审，只写批注不拦截交付',
}

export const PIPELINE = [
  'parse',
  'validate',
  'match',
  'profile',
  'plan',
  'generate',
  'review',
] as const

export function nodeLabel(node: string): string {
  return NODE_LABEL[node] ?? node
}

export function pipelineSize(): number {
  return PIPELINE.length
}

export function statusLabel(status: string): string {
  return STATUS_META[status]?.label ?? status
}

export function shortId(id: string | null | undefined, length = 8): string {
  return id ? id.slice(0, length) : '—'
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 19)
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return value.slice(0, 10)
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return ''
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function formatDuration(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return minutes > 0 ? `${minutes} 分 ${seconds.toString().padStart(2, '0')} 秒` : `${seconds} 秒`
}

export function percent(part: number, total: number): number {
  if (!total) return 0
  return Math.round((part / total) * 100)
}

/** 简历库的行叫 `name`，报告头的快照叫 `resume_name`——同一个标签在两处都能用。 */
export function resumeLabel(resume: {
  id?: string
  name?: string
  file_name?: string
  resume_name?: string
  resume_file_name?: string
}): string {
  return (
    resume.resume_name ||
    resume.name ||
    resume.resume_file_name ||
    resume.file_name ||
    shortId(resume.id)
  )
}

export function reportPairLabel(
  report: Pick<
    Interview,
    'resume_name' | 'resume_file_name' | 'job_title' | 'job_company' | 'id'
  >,
): string {
  const resume = report.resume_name || report.resume_file_name || '未命名简历'
  const job = report.job_title || '未记录岗位'
  return report.job_company ? `${resume} × ${job}（${report.job_company}）` : `${resume} × ${job}`
}

export function issuesByIndex(
  issues: ReviewIssue[] | null | undefined,
): Map<number, ReviewIssue[]> {
  const grouped = new Map<number, ReviewIssue[]>()
  for (const issue of issues ?? []) {
    if (issue.index === null || issue.index === undefined) continue
    const bucket = grouped.get(issue.index)
    if (bucket) bucket.push(issue)
    else grouped.set(issue.index, [issue])
  }
  return grouped
}

export function initials(label: string): string {
  const text = label.trim()
  return text ? text.slice(0, 1).toUpperCase() : '?'
}
