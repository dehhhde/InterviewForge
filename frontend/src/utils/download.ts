/**
 * 导出一份报告的 Markdown〔T-306 / BACKLOG「两套分叉的 Markdown 导出」的前端那一半〕。
 *
 * 这里**不渲染任何东西**：内容来自 `GET /interviews/{id}/export.md`，它和命令行脚本
 * 调的是同一个 `export_service.render_markdown()`，所以"两条路内容一致"由服务端保证。
 * 原先 `utils/markdown.ts` 那份页面实现缺 profile / plan / trace，同名不同内容——
 * 那个文件已删除，本模块只负责"让浏览器下载一个 URL"。
 * 文件名不在这里拼：服务端在 `Content-Disposition` 里给（简历名与岗位名都经过清洗）。
 */

import { apiBase } from '@/api/http'

export function interviewExportUrl(interviewId: string): string {
  return `${apiBase()}/interviews/${encodeURIComponent(interviewId)}/export.md`
}

/**
 * 用一次性 `<a>` 触发下载。同源 + 响应头是 `attachment`，所以浏览器直接存盘，
 * 不会导航——不需要 `target="_blank"`，也不需要先把响应读进内存再造 blob。
 */
export function downloadReportMarkdown(interviewId: string): void {
  const link = document.createElement('a')
  link.href = interviewExportUrl(interviewId)
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
}
