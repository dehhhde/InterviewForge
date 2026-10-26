import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import ConsoleLayout from '@/layouts/ConsoleLayout.vue'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    /** 详情页在侧边栏里高亮哪个入口 */
    activeMenu?: string
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: ConsoleLayout,
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '总览' },
      },
      {
        path: 'generate',
        name: 'generate',
        component: () => import('@/views/GenerateView.vue'),
        meta: { title: '生成面试题' },
      },
      {
        path: 'reports',
        name: 'reports',
        component: () => import('@/views/ReportsView.vue'),
        meta: { title: '报告中心' },
      },
      {
        path: 'reports/:id',
        name: 'report-detail',
        component: () => import('@/views/ReportDetailView.vue'),
        meta: { title: '报告详情', activeMenu: '/reports' },
      },
      {
        path: 'resumes',
        name: 'resumes',
        component: () => import('@/views/ResumesView.vue'),
        meta: { title: '简历库' },
      },
      {
        path: 'jobs',
        name: 'jobs',
        component: () => import('@/views/JobsView.vue'),
        meta: { title: '岗位库' },
      },
      {
        path: 'system',
        name: 'system',
        component: () => import('@/views/SystemView.vue'),
        meta: { title: '系统状态' },
      },
      {
        path: ':pathMatch(.*)*',
        name: 'not-found',
        component: () => import('@/views/NotFoundView.vue'),
        meta: { title: '页面不存在' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${to.meta.title} · InterviewForge`
    : 'InterviewForge · 面试题工作台'
})

export default router
