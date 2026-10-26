<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { Expand, Fold, Menu, Moon, Reading, Sunny } from '@element-plus/icons-vue'

import HealthBadge from '@/components/HealthBadge.vue'
import SideNav from '@/components/SideNav.vue'
import { useAppStore } from '@/stores/app'
import { APP_VERSION } from '@/utils/version'

const store = useAppStore()
const route = useRoute()

const crumbTitle = computed(() => route.meta.title ?? '控制台')
const year = new Date().getFullYear()

let healthTimer: number | undefined
onMounted(() => {
  void store.checkHealth()
  healthTimer = window.setInterval(() => void store.checkHealth(), 60_000)
})
onBeforeUnmount(() => {
  if (healthTimer) window.clearInterval(healthTimer)
})
</script>

<template>
  <div class="console-shell">
    <aside class="sidebar" :class="{ 'is-collapsed': store.sidebarCollapsed }">
      <div class="sidebar-brand">
        <div class="sidebar-logo">IF</div>
        <div v-if="!store.sidebarCollapsed" class="sidebar-brand-text">
          <strong>InterviewForge</strong>
          <span>AI 面试题工作台</span>
        </div>
      </div>
      <div class="sidebar-nav">
        <SideNav :collapsed="store.sidebarCollapsed" />
      </div>
      <div class="sidebar-foot">
        <div class="sidebar-health">
          <div class="sidebar-health-row">
            <span
              class="sidebar-status-dot"
              :class="store.healthState"
              aria-hidden="true"
            />
            <span class="sidebar-health-text">
              {{ store.healthState === 'online' ? '服务在线' : store.healthState === 'checking' ? '检测中' : '服务不可达' }}
            </span>
          </div>
          <small v-if="!store.sidebarCollapsed">
            {{ store.health?.env ?? 'unknown' }} · v{{ APP_VERSION }}
          </small>
        </div>
      </div>
    </aside>

    <div class="console-main">
      <header class="console-header">
        <button
          class="header-icon-btn desktop-only"
          :title="store.sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
          @click="store.toggleSidebar()"
        >
          <el-icon><Expand v-if="store.sidebarCollapsed" /><Fold v-else /></el-icon>
        </button>
        <button
          class="header-icon-btn mobile-only"
          title="打开导航"
          @click="store.mobileMenuOpen = true"
        >
          <el-icon><Menu /></el-icon>
        </button>

        <div class="header-crumbs">
          <span>工作台</span>
          <span>/</span>
          <strong>{{ crumbTitle }}</strong>
        </div>

        <div class="header-spacer" />

        <div class="header-actions">
          <HealthBadge />
          <el-tooltip :content="store.isDark ? '切换到亮色' : '切换到暗色'" placement="bottom">
            <button class="header-icon-btn" @click="store.toggleTheme()">
              <el-icon><Sunny v-if="store.isDark" /><Moon v-else /></el-icon>
            </button>
          </el-tooltip>
          <el-tooltip content="接口文档" placement="bottom">
            <a class="header-icon-btn" href="/docs" target="_blank" rel="noreferrer">
              <el-icon><Reading /></el-icon>
            </a>
          </el-tooltip>
          <el-dropdown trigger="click">
            <div class="header-user">
              <span class="header-avatar">AD</span>
              <span class="header-user-text">
                <strong>管理员</strong>
                <span>无鉴权模式</span>
              </span>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>当前未启用登录 / 权限</el-dropdown-item>
                <el-dropdown-item @click="store.toggleTheme()">切换主题</el-dropdown-item>
                <el-dropdown-item divided @click="store.checkHealth()">重新检测服务</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="console-body">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>

      <footer class="console-foot">
        InterviewForge · FastAPI + LangGraph + SQLite · {{ year }}
      </footer>
    </div>

    <el-drawer
      v-model="store.mobileMenuOpen"
      direction="ltr"
      size="258px"
      :with-header="false"
      class="mobile-nav-drawer"
    >
      <div class="mobile-brand">
        <span class="sidebar-logo">IF</span>
        <div class="sidebar-brand-text">
          <strong>InterviewForge</strong>
          <span>AI 面试题工作台</span>
        </div>
      </div>
      <div class="mobile-nav-body" @click="store.mobileMenuOpen = false">
        <SideNav />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.desktop-only {
  display: grid;
}

@media (max-width: 1024px) {
  .desktop-only {
    display: none;
  }
}

.sidebar-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #8a94a6;
}

.sidebar-status-dot.online {
  background: #34d399;
  box-shadow: 0 0 0 3px rgba(52, 211, 153, 0.18);
}

.sidebar-status-dot.offline {
  background: #f87171;
  box-shadow: 0 0 0 3px rgba(248, 113, 113, 0.18);
}

.mobile-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 68px;
  padding: 0 18px;
  border-bottom: 1px solid var(--sidebar-line);
}

.mobile-brand .sidebar-brand-text strong {
  display: block;
  font-size: 15px;
  color: #fff;
}

.mobile-brand .sidebar-brand-text span {
  font-size: 11px;
  color: rgba(232, 238, 250, 0.55);
}

.mobile-nav-body {
  padding: 14px 12px;
}
</style>

<style>
.mobile-nav-drawer .el-drawer__body {
  padding: 0;
  background: linear-gradient(175deg, var(--sidebar-bg) 0%, var(--sidebar-bg-2) 100%);
}

.mobile-nav-drawer .sidebar-brand-text strong {
  color: #fff;
}
</style>
