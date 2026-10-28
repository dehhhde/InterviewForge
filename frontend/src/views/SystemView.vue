<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  CircleCheck,
  CircleClose,
  Connection,
  Cpu,
  Document,
  Moon,
  Refresh,
  Sunny,
} from '@element-plus/icons-vue'

import { apiBase } from '@/api/http'
import PageHeader from '@/components/PageHeader.vue'
import StatCard from '@/components/StatCard.vue'
import { useAppStore } from '@/stores/app'
import { NODE_DESCRIPTION, NODE_LABEL, PIPELINE } from '@/utils/format'
import { POLL_INTERVAL_MS } from '@/utils/task'
import { APP_VERSION, BUILD_TIME } from '@/utils/version'

const store = useAppStore()

const checking = ref(false)
const latency = ref<number | null>(null)

const checkedAt = computed(() =>
  store.healthCheckedAt ? new Date(store.healthCheckedAt).toLocaleString('zh-CN') : '—',
)

const backendOrigin = computed(() => {
  // dev server 在 3000，后端在 8000：/docs 没走 Vite 的 /api 代理，得指向真实后端。
  if (import.meta.env.DEV && location.port === '3000') {
    return `${location.protocol}//${location.hostname}:8000`
  }
  return location.origin
})

const browserOrigin = computed(() => location.origin)

const endpoints = [
  { method: 'GET', path: '/api/v1/health', note: '存活探针，返回 app 与 env' },
  { method: 'POST', path: '/api/v1/resumes/upload', note: '上传简历，202 返回 resume_id + task_id' },
  { method: 'GET', path: '/api/v1/resumes/list', note: '简历列表：精简行 + total，不含 parsed 正文' },
  { method: 'GET', path: '/api/v1/resumes/{id}', note: '单份简历详情' },
  { method: 'PUT', path: '/api/v1/resumes/{id}', note: '只允许改展示名 name' },
  { method: 'DELETE', path: '/api/v1/resumes/{id}', note: '名下有报告先 409，?force=true 级联删除' },
  { method: 'GET', path: '/api/v1/jobs', note: '岗位列表：精简行 + total，正文只给节选' },
  { method: 'POST', path: '/api/v1/jobs', note: '新建岗位；自然键冲突返 409' },
  { method: 'POST', path: '/api/v1/jobs/upload', note: '从 txt / md / pdf / docx 导入 JD' },
  { method: 'POST', path: '/api/v1/jobs/inspect-image', note: '图片识别草稿，不落库' },
  { method: 'GET', path: '/api/v1/jobs/{id}', note: '岗位详情' },
  { method: 'PUT', path: '/api/v1/jobs/{id}', note: '更新岗位，JD 变更会重派生技能' },
  { method: 'DELETE', path: '/api/v1/jobs/{id}', note: '删除岗位，历史报告保留快照' },
  { method: 'POST', path: '/api/v1/interviews/generate', note: '发起出题，202 返回 interview_id + task_id；auto_fix=true 时点名的档重出一轮' },
  { method: 'GET', path: '/api/v1/interviews/list', note: '报告列表：精简行 + total，不含题目行' },
  { method: 'GET', path: '/api/v1/interviews/{id}', note: '完整报告：题目 + 批注 + 计划' },
  {
    method: 'GET',
    path: '/api/v1/interviews/{id}/export.md',
    note: '服务端渲染的 Markdown 全文，带下载文件名',
  },
  {
    method: 'GET',
    path: '/api/v1/tasks/{task_id}',
    note: `任务进度，出题页每 ${POLL_INTERVAL_MS / 1000} 秒轮询`,
  },
]

async function check(): Promise<void> {
  checking.value = true
  const started = performance.now()
  try {
    await store.checkHealth()
  } finally {
    latency.value = Math.round(performance.now() - started)
    checking.value = false
  }
}

onMounted(() => {
  if (!store.healthCheckedAt) void check()
})
</script>

<template>
  <div class="page">
    <PageHeader
      eyebrow="SYSTEM"
      title="系统状态"
      subtitle="控制台与 API 同源，只做展示与轮询。这里汇总连接状态、构建信息和后端暴露的读写端点。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="checking" @click="check">重新检测</el-button>
        <el-button :icon="store.isDark ? Sunny : Moon" @click="store.toggleTheme">
          {{ store.isDark ? '切换到亮色' : '切换到暗色' }}
        </el-button>
      </template>
    </PageHeader>

    <div class="grid grid-4">
      <StatCard
        label="API 状态"
        :value="store.healthState === 'online' ? '在线' : store.healthState === 'offline' ? '离线' : '检测中'"
        :hint="store.health ? `${store.health.app} · ${store.health.env}` : store.healthError || '尚未完成检测'"
        :tone="store.healthState === 'online' ? 'green' : 'amber'"
        :icon="Connection"
      />
      <StatCard
        label="响应耗时"
        :value="latency === null ? '—' : `${latency} ms`"
        :hint="`最近检测：${checkedAt}`"
        tone="brand"
        :icon="Cpu"
      />
      <StatCard
        label="控制台版本"
        :value="APP_VERSION"
        :hint="`构建于 ${BUILD_TIME.slice(0, 19).replace('T', ' ')}`"
        tone="violet"
        :icon="Document"
      />
      <StatCard
        label="接口根地址"
        :value="apiBase()"
        hint="由 VITE_API_BASE 决定；生产默认同源 /api/v1"
        tone="green"
        :icon="Connection"
      />
    </div>

    <div class="split">
      <div class="card">
        <div class="card-head">
          <div>
            <h3>运行时信息</h3>
            <p>配置来自后端 <code>settings</code> 与环境变量；控制台不读取任何密钥。</p>
          </div>
          <span
            class="pill"
            :class="store.healthState === 'online' ? 'ok' : store.healthState === 'offline' ? 'bad' : 'muted'"
          >
            <el-icon>
              <CircleCheck v-if="store.healthState === 'online'" />
              <CircleClose v-else-if="store.healthState === 'offline'" />
              <Connection v-else />
            </el-icon>
            {{ store.healthState === 'online' ? '已连接' : store.healthState === 'offline' ? '未连接' : '检测中' }}
          </span>
        </div>

        <div class="kv-row">
          <span>健康检查</span>
          <span><code>{{ apiBase() }}/health</code></span>
        </div>
        <div class="kv-row">
          <span>应用名</span>
          <span>{{ store.health?.app || '—' }}</span>
        </div>
        <div class="kv-row">
          <span>运行环境</span>
          <span>{{ store.health?.env || '—' }}</span>
        </div>
        <div class="kv-row">
          <span>浏览器地址</span>
          <span>{{ browserOrigin }}</span>
        </div>
        <div class="kv-row">
          <span>本地主题</span>
          <span>{{ store.isDark ? '暗色（localStorage 持久化）' : '亮色（localStorage 持久化）' }}</span>
        </div>

        <div v-if="store.healthError" class="alert-inline bad" role="alert" style="margin-top: 14px">
          {{ store.healthError }}
        </div>

        <div class="toolbar" style="margin-top: 18px">
          <el-button tag="a" :href="`${backendOrigin}/docs`" target="_blank" rel="noreferrer">
            打开 Swagger 文档
          </el-button>
          <el-button tag="a" :href="`${backendOrigin}/openapi.json`" target="_blank" rel="noreferrer">
            查看 OpenAPI
          </el-button>
          <el-button tag="a" :href="`${backendOrigin}/api/v1/health`" target="_blank" rel="noreferrer">
            原始健康检查
          </el-button>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <h3>工作流节点</h3>
            <p>七节点在后台协程中运行，进度写进 <code>task.progress.steps</code>。</p>
          </div>
          <span class="pill live">LangGraph</span>
        </div>
        <ol class="node-list">
          <li v-for="(node, index) in PIPELINE" :key="node">
            <span class="node-index">{{ index + 1 }}</span>
            <div>
              <b>{{ NODE_LABEL[node] }}</b>
              <small>{{ NODE_DESCRIPTION[node] }}</small>
            </div>
            <code>{{ node }}</code>
          </li>
        </ol>
      </div>
    </div>

    <div class="card flush">
      <div class="card-head" style="padding: 20px 22px 0; margin-bottom: 12px">
        <div>
          <h3>后端端点</h3>
          <p>控制台用到的全部读写接口；所有错误统一为 <code>{ "code", "message", "details" }</code> 的 error 信封。</p>
        </div>
        <span class="pill muted">{{ endpoints.length }} 个</span>
      </div>
      <el-table :data="endpoints" size="small">
        <el-table-column label="方法" width="100">
          <template #default="{ row }">
            <span class="method" :class="row.method.toLowerCase()">{{ row.method }}</span>
          </template>
        </el-table-column>
        <el-table-column label="路径" min-width="280">
          <template #default="{ row }"><code>{{ row.path }}</code></template>
        </el-table-column>
        <el-table-column prop="note" label="说明" min-width="320" />
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.node-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.node-list li {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  padding: 10px 12px;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}

.node-list li > div {
  flex: 1;
  min-width: 0;
}

.node-list b {
  display: block;
  font-size: 13px;
  font-weight: 620;
}

.node-list small {
  display: block;
  margin-top: 1px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-3);
}

.node-list code {
  color: var(--text-3);
}

.node-index {
  display: grid;
  flex: 0 0 22px;
  place-items: center;
  width: 22px;
  height: 22px;
  margin-top: 1px;
  font-size: 11px;
  font-weight: 700;
  color: var(--brand);
  background: var(--brand-soft);
  border-radius: 50%;
}

.method {
  display: inline-flex;
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border-radius: 5px;
}

.method.get {
  color: var(--live);
  background: var(--live-soft);
}

.method.post {
  color: var(--ok);
  background: var(--ok-soft);
}

.method.put {
  color: var(--warn);
  background: var(--warn-soft);
}

.method.delete {
  color: var(--bad);
  background: var(--bad-soft);
}
</style>
