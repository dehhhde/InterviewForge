import { getTask } from '@/api'
import { isApiFailure, type ApiFailure } from '@/api/http'
import type { Task } from '@/api/types'

const TERMINAL: ReadonlySet<string> = new Set(['completed', 'failed'])

interface PollOptions {
  interval?: number
  maxRounds?: number
  onError?: (error: ApiFailure) => void
}

export interface TaskPoller {
  run: (taskId: string, onUpdate: (task: Task) => void) => Promise<Task | null>
  stop: () => void
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 轮询票号：重新发起或主动停止时递增，旧循环下一轮发现自己过期就退出。
 * 不这么做两次轮询会抢着写同一个进度面板。
 */
export function createTaskPoller(options: PollOptions = {}): TaskPoller {
  const interval = options.interval ?? 2000
  const maxRounds = options.maxRounds ?? 900
  let token = 0

  return {
    stop() {
      token += 1
    },
    async run(taskId, onUpdate) {
      const mine = ++token
      let consecutiveErrors = 0

      for (let round = 0; round < maxRounds; round += 1) {
        if (mine !== token) return null
        try {
          const task = await getTask(taskId)
          consecutiveErrors = 0
          if (mine !== token) return null
          onUpdate(task)
          if (TERMINAL.has(task.status)) return task
        } catch (error) {
          consecutiveErrors += 1
          if (isApiFailure(error)) options.onError?.(error)
          // 偶发一次读不到就继续等；连续失败说明观测窗口已经断了，返回让页面提示刷新确认。
          if (consecutiveErrors >= 3) return null
        }
        await sleep(interval)
      }
      return null
    },
  }
}
