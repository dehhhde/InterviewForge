import type { Interview, ReviewIssue } from '@/api/types'
import {
  DIFFICULTY_LABEL,
  SOURCE_LABEL,
  formatDateTime,
  issueCount,
  issuesByIndex,
  reportFileName,
  reportPairLabel,
} from '@/utils/format'

function issueLine(issue: ReviewIssue): string {
  const index = issue.index === null || issue.index === undefined ? '整批' : `第 ${issue.index + 1} 题`
  return `- ${index}（${issue.origin || '?'}）：${issue.problem || ''}${
    issue.fix ? ` 建议：${issue.fix}` : ''
  }`
}

/** 页面导出的"面试用卷"：只含 InterviewOut 能给的字段，不掺 checkpoint 终态。 */
export function toMarkdown(report: Interview): string {
  const flagged = issuesByIndex(report.review_issues)
  const lines: string[] = [
    `# 面试题 · ${reportPairLabel(report)}`,
    '',
    `> 报告 id \`${report.id}\`｜状态 ${report.status}｜题目 ${report.questions.length} 道｜` +
      `批注 ${issueCount(report.review_issues)} 条｜生成时间 ${formatDateTime(report.created_at)}`,
    '',
  ]

  if (!report.questions.length) {
    lines.push('这份报告没有题目行（后台任务没跑到收尾，或仍在生成中）。', '')
  }

  report.questions.forEach((item, index) => {
    const notes = flagged.get(index) ?? []
    lines.push(
      `## 第 ${index + 1} 题 · ${DIFFICULTY_LABEL[item.difficulty] ?? item.difficulty} · ${
        SOURCE_LABEL[item.source] ?? item.source
      }${notes.length ? ` · 批注 ${notes.length} 条` : ''}`,
      '',
      `**题干**：${item.stem}`,
      '',
      `**参考答案**：${item.reference_answer}`,
      '',
      '**考察点**',
      ...item.checkpoints.map((checkpoint) => `- ${checkpoint}`),
      '',
    )
    if (item.rubric.length) {
      lines.push('**打分标准**', ...item.rubric.map((rubric) => `- ${rubric}`), '')
    }
    if (item.follow_ups.length) {
      lines.push('**追问**', ...item.follow_ups.map((followUp) => `- ${followUp}`), '')
    }
    if (item.evidence) {
      lines.push(`**依据出处**：${item.evidence}`, '')
    }
    if (notes.length) {
      lines.push(
        '**评审批注**',
        ...notes.map(
          (issue) =>
            `- ${issue.problem || ''}${issue.fix ? ` 建议：${issue.fix}` : ''}`,
        ),
        '',
      )
    }
  })

  if (report.review_issues.length) {
    lines.push('## 评审批注', ...report.review_issues.map(issueLine), '')
  }
  return lines.join('\n')
}

export function downloadMarkdown(report: Interview): void {
  const blob = new Blob([toMarkdown(report)], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = reportFileName(report)
  link.click()
  URL.revokeObjectURL(url)
}
