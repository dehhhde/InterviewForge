import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { fetchHealth } from '@/api'
import type { Health } from '@/api/types'

type Theme = 'light' | 'dark'
type HealthState = 'checking' | 'online' | 'offline'

const THEME_KEY = 'if-console-theme'
const COLLAPSE_KEY = 'if-console-collapsed'

function initialTheme(): Theme {
  const saved = localStorage.getItem(THEME_KEY)
  return saved === 'dark' ? 'dark' : 'light'
}

export const useAppStore = defineStore('app', () => {
  const theme = ref<Theme>(initialTheme())
  const sidebarCollapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1')
  const mobileMenuOpen = ref(false)

  const health = ref<Health | null>(null)
  const healthState = ref<HealthState>('checking')
  const healthError = ref('')
  const healthCheckedAt = ref<number | null>(null)

  const isDark = computed(() => theme.value === 'dark')

  function applyTheme(): void {
    document.documentElement.classList.toggle('dark', theme.value === 'dark')
    localStorage.setItem(THEME_KEY, theme.value)
  }

  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    applyTheme()
  }

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
    localStorage.setItem(COLLAPSE_KEY, sidebarCollapsed.value ? '1' : '0')
  }

  async function checkHealth(): Promise<void> {
    healthState.value = 'checking'
    try {
      health.value = await fetchHealth()
      healthState.value = 'online'
      healthError.value = ''
    } catch (error) {
      health.value = null
      healthState.value = 'offline'
      healthError.value = (error as Error).message
    } finally {
      healthCheckedAt.value = Date.now()
    }
  }

  return {
    theme,
    isDark,
    sidebarCollapsed,
    mobileMenuOpen,
    health,
    healthState,
    healthError,
    healthCheckedAt,
    applyTheme,
    toggleTheme,
    toggleSidebar,
    checkHealth,
  }
})
