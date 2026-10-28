import { onBeforeUnmount, watch, type Ref } from 'vue'

/**
 * 关键词框 → 服务端搜索〔T-307〕。过滤判据搬到 `q` 参数以后，"每敲一个字发一次请求"
 * 就把本地过滤的成本换成了持续捶服务端，所以输入要先并段。
 *
 * 只做防抖，不做请求取消：结果总是"最后一次 `run` 的那批行"，视图自己负责 loading 态；
 * 加一套 token 只会让三个列表页都得抄一遍同样的东西。
 * `onBeforeUnmount` 要求本函数在 setup 期间调用——三个页面都满足。
 */
export function useDebouncedSearch(
  source: Ref<string>,
  run: (term: string) => void,
  delay = 350,
): void {
  let timer: number | null = null

  watch(source, (value) => {
    if (timer !== null) window.clearTimeout(timer)
    timer = window.setTimeout(() => {
      timer = null
      run(value.trim())
    }, delay)
  })

  onBeforeUnmount(() => {
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  })
}
