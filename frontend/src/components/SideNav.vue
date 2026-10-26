<script setup lang="ts">
import type { Component } from 'vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  Briefcase,
  Document,
  MagicStick,
  Monitor,
  Odometer,
  Tickets,
} from '@element-plus/icons-vue'

defineProps<{ collapsed?: boolean }>()

interface NavItem {
  path: string
  label: string
  icon: Component
}

interface NavGroup {
  label: string
  items: NavItem[]
}

const groups: NavGroup[] = [
  {
    label: '工作台',
    items: [
      { path: '/dashboard', label: '总览', icon: Odometer },
      { path: '/generate', label: '生成面试题', icon: MagicStick },
    ],
  },
  {
    label: '资产与产出',
    items: [
      { path: '/reports', label: '报告中心', icon: Document },
      { path: '/resumes', label: '简历库', icon: Tickets },
      { path: '/jobs', label: '岗位库', icon: Briefcase },
    ],
  },
  {
    label: '系统',
    items: [{ path: '/system', label: '系统状态', icon: Monitor }],
  },
]

const route = useRoute()
const activePath = computed(() => (route.meta.activeMenu as string | undefined) ?? route.path)
</script>

<template>
  <div>
    <div v-for="group in groups" :key="group.label" class="nav-group">
      <p v-if="!collapsed" class="nav-group-label">{{ group.label }}</p>
      <el-tooltip
        v-for="item in group.items"
        :key="item.path"
        :content="item.label"
        placement="right"
        :disabled="!collapsed"
      >
        <router-link
          :to="item.path"
          class="nav-item"
          :class="{ active: activePath === item.path }"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span v-if="!collapsed">{{ item.label }}</span>
        </router-link>
      </el-tooltip>
    </div>
  </div>
</template>
