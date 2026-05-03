/**
 * Cross-cutting auth-state-change event bus.
 *
 * Why this exists (Bug 1/2/3/4 fix):
 *
 * - 之前 `applyTokenPair` 仅写 `userRef.value = …`、`clearAuth` 仅清 token，
 *   登录后 `router.beforeEach` bootstrap guard 不会再触发（once-per-session
 *   sessionStorage flag），导致首屏 store / persistentReactive 全空（默认
 *   provider/model 看着像丢了，发送按钮 disable，工作区 nav 不刷新）；登出
 *   后本地 IDB 残留旧账号数据 + 各 `<table>.server.ts` 的 `lastVersion` 仍
 *   停在旧账号 cursor，下个账号登录后被错误判断 "无新数据" 不拉。
 *
 * - 集中化的 emit + listener 比让 `auth.backend.ts` 直接 import router /
 *   10 个 server 模块 / db 干净得多：
 *     * 避免 auth.backend → router 循环（router 已经 import authSource）
 *     * 避免 auth.backend 强耦合到每张新加的 server 表
 *     * 单测 / e2e 可以 subscribe 一个 spy 直接断言事件次数
 *
 * 模块负载：
 *   - `auth.backend.ts` ① applyTokenPair 末尾 emit 'login'（仅当 user 由
 *     null/undefined 推到 truthy）② clearAuth 起始 emit 'logout'（仅当之前
 *     logged in）。
 *   - `router/index.ts` listen 'login' → triggerBootstrapNow；listen
 *     'logout' → 清 bootstrap session flag + inflight。
 *   - 每张 `<table>.server.ts` listen all → reset `lastVersion=0` +
 *     unsubscribe realtime + scopedPull.reset。
 *
 * 注意：listener 调用是同步顺序、同步抛出会冒到 emit 调用方。各 listener
 * 应该在自己内部吞异常或转 fire-and-forget 异步任务，避免一个 listener
 * 把整条 emit 链炸掉。
 */
export type AuthChangeReason = 'login' | 'logout'
export type AuthChangeListener = (reason: AuthChangeReason) => void

const listeners = new Set<AuthChangeListener>()

export function subscribeAuthChange(listener: AuthChangeListener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function emitAuthChange(reason: AuthChangeReason): void {
  // Snapshot before iterating — listener body may unsubscribe itself or
  // others; we still want the original set to fire exactly once.
  for (const fn of Array.from(listeners)) {
    try {
      fn(reason)
    } catch (err) {
      // Don't let one broken listener tear down the rest of the chain.
      console.warn('[auth-events] listener threw on', reason, err)
    }
  }
}
