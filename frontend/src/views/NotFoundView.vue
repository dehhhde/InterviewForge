<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ArrowLeft, HomeFilled, MagicStick } from '@element-plus/icons-vue'

const router = useRouter()
</script>

<template>
  <div class="page">
    <div class="card notfound">
      <div class="notfound-code">404</div>
      <h1>页面不存在</h1>
      <p>
        这个地址不在控制台的路由表里。可能是链接拼写有误，或页面已经改名。
        控制台只承载展示与轮询，所有业务事实都在后端 API 里。
      </p>
      <div class="toolbar" style="justify-content: center">
        <el-button :icon="HomeFilled" type="primary" @click="router.push('/dashboard')">
          回到工作台
        </el-button>
        <el-button :icon="MagicStick" @click="router.push('/generate')">发起出题</el-button>
        <el-button :icon="ArrowLeft" @click="router.back()">返回上一页</el-button>
      </div>
      <code class="notfound-path">{{ router.currentRoute.value.fullPath }}</code>
    </div>
  </div>
</template>

<style scoped>
.notfound {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 64px 24px;
  text-align: center;
}

.notfound-code {
  font-size: 88px;
  font-weight: 800;
  line-height: 1;
  color: transparent;
  letter-spacing: -0.05em;
  background: linear-gradient(135deg, #5b7cfa, #8b5cf6);
  background-clip: text;
  -webkit-background-clip: text;
}

.notfound h1 {
  font-size: 22px;
}

.notfound p {
  max-width: 560px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-3);
}

.notfound-path {
  padding: 3px 10px;
  color: var(--text-3);
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: 6px;
}
</style>
