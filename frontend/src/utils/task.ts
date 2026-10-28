import { getTask } from '@/api'
import { isApiFailure, type ApiFailure } from '@/api/http'
import type { Task } from '@/api/types'

const TERMINAL: ReadonlySet<string> = new Set(['completed', 'failed'])

/**
 * 节奏常量对外可见〔T-308〕：页面文案里那句"每 N 秒轮询一次"从这里取。
 * 原先 `index.html` 写死"80 秒 / 每 2 秒"、代码是另一套，两处各说各话。
 */
export const POLL_INTERVAL_MS = 2000

/** 静默刷新的节奏比任务轮询慢：列表页没有"下一步该发生什么"的期待，只需要不太迟地发现状态变了。 */
export const REFRESH_INTERVAL_MS = 8000

/** 退避封顶：2s→4s→8s→10s。再往上就不是退避，是"这个页面已经不打算给你结果了"。 */
export const MAX_INTERVAL_MS = 10_000

/** 连续失败这么多次就熔断——继续捶只会把"读不到"变成"读得更久"。 */
const MAX_CONSECUTIVE_ERRORS = 3

const MAX_ROUNDS = 900

interface PollOptions {
  interval?: number
  maxInterval?: number
  maxRounds?: number
  onError?: (error: ApiFailure) => void
}

export interface TaskPoller {
  run: (taskId: string, onUpdate: (task: Task) => void) => Promise<Task | null>
  stop: () => void
}

export interface RefreshLoop {
  start: () => void
  stop: () => void
}

function backoff(base: number, max: number, consecutiveErrors: number): number {
  if (consecutiveErrors <= 0) return base
  return Math.min(max, base * 2 ** consecutiveErrors)
}

/**
 * 可中断的等待〔T-305〕：`stop()` 只 bump 票号，正在 sleep 的那一轮要等满间隔才发现自己过期，
 * 于是"停止轮询"点下去界面不动几秒。把 resolve 交出去，stop 时立刻放行——
 * 放行后循环照样在下一轮的票号检查处退出，语义不变。
 */
function nap(ms: number, onWake: (wake: () => void) => void): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ms)
    onWake(() => {
      window.clearTimeout(timer)
      resolve()
    })
  })
}

/**
 * 轮询票号：重新发起或主动停止时递增，旧循环下一轮发现自己过期就退出。
 * 不这么做两次轮询会抢着写同一个进度面板。
 */
export function createTaskPoller(options: PollOptions = {}): TaskPoller {
  const interval = options.interval ?? POLL_INTERVAL_MS
  const maxInterval = options.maxInterval ?? MAX_INTERVAL_MS
  const maxRounds = options.maxRounds ?? MAX_ROUNDS
  let token = 0
  let wake: (() => void) | null = null

  return {
    stop() {
      token += 1
      const release = wake
      wake = null
      release?.()
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
          if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) return null
        }
        // 成功读到就回到基准间隔：跟手是进度面板的第一价值；只有失败那一轮往后退。
        await nap(backoff(interval, maxInterval, consecutiveErrors), (fn) => {
          wake = fn
        })
        wake = null
      }
      return null
    },
  }
}

/**
 * 列表的静默刷新循环〔T-305〕：报告页原先自己开 `setInterval(8000)`，和任务轮询是两套节奏、
 * 两套失败处理——后端断掉时它照样每 8 秒捶一次，而且页面完全不知道。
 * 这里复用同一条退避与熔断规则，熔断后走 `onStop`，由页面摆出"重试"而不是继续假装在刷新。
 *
 * `more` 是"这一轮值不值得拉"（报告页：只在还有 running 行时拉），它只在本地判断，
 * 不发请求——空闲列表本来就不该产生流量。
 */
export function createRefreshLoop(
  action: () => Promise<void>,
  options: {
    interval?: number
    maxInterval?: number
    more?: () => boolean
    onStop?: (error: unknown) => void
  } = {},
): RefreshLoop {
  const interval = options.interval ?? REFRESH_INTERVAL_MS
  const maxInterval = options.maxInterval ?? MAX_INTERVAL_MS
  let timer: number | null = null
  let running = false
  let consecutiveErrors = 0

  async function tick(): Promise<void> {
    timer = null
    if (!running) return
    if (options.more?.() ?? true) {
      try {
        await action()
        consecutiveErrors = 0
      } catch (error) {
        consecutiveErrors += 1
        if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
          running = false
          options.onStop?.(error)
          return
        }
      }
    }
    schedule(backoff(interval, maxInterval, consecutiveErrors))
  }

  function schedule(delay: number): void {
    timer = window.setTimeout(() => void tick(), delay)
  }

  return {
    start() {
      if (running) return
      running = true
      consecutiveErrors = 0
      schedule(interval)
    },
    stop() {
      running = false
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
    },
  }
}
