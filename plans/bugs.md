# 已知 Bug 清单

## 状态图例

- ✅ 已修复 — 修复已落地 + 自动化 spec 覆盖
- 🚧 修复中 — 已分配，正在修复
- ❌ 未修复 — 已确认现象，未排程

---

## Bug 1：发送按钮灰置，无法发送消息

- **状态**: ✅ 已修复
- **现象**：在对话输入框里输入文字后，右侧发送按钮一直保持灰色 disabled 状态，点不动。
- **触发场景**：登录后进入会话页直接输入。
- **临时绕过**：手动 F5 刷新页面后按钮恢复正常，可以发送。
- **关联**：与 Bug 4 一同出现（首屏渲染未把 store / 默认模型加载齐，发送按钮的 enabled 判断依赖默认模型存在）。

### 修复记录
- **日期**: 2026-05-04
- **根因**: `applyTokenPair` 不主动触发 bootstrap，登录后 `router.beforeEach` once-per-session guard 不会重跑；DialogView 的 `inputMessageContent` 依赖 `chain.value.at(-1)` 取出 inputing message，但 IDB 里 messages 全空 → `chain.at(-1) = '$root'` → `messageMap[$root]` 不存在 → `inputMessageContent = undefined` → `inputEmpty = true` → 发送按钮永远 disabled。typing 也是 no-op：`updateInputText` 调 `repos.messages.update(undefined, ...)` → cache.get 返 undefined → 返回 0 不写。
- **核心改动**:
  - `src/data/auth-events.ts`（新建）：纯事件 bus（`subscribeAuthChange / emitAuthChange`）
  - `src/data/auth.backend.ts:134-136`：`applyTokenPair` 末尾 emit 'login'（仅 null→truthy 跃迁）
  - `src/router/index.ts:155-167 + 177-192`：`triggerBootstrapNow` + auth-events 'login' listener
- **判据映射**:
  - 登录后无需 reload 立即可发送（IDB roundtrip via updateInputText 证明 chain resolved） → spec: `tests/e2e/bugs/bug1-send-button-after-login.spec.ts::bug1 [providers-rest|realtime-ws] login via UI → no reload → send button enabled`
- **故障注入红测**: 注释 `src/data/auth.backend.ts:134-136` 的 `if (!wasLoggedIn) emitAuthChange('login')` → spec 红 ✅（`seeded workspace=... must all land in IDB within 3s post-login: Expected: true Received: false`）→ 恢复 → 绿 ✅
- **关联 bug 顺带消除**: Bug 3 + Bug 4（同源，bootstrap-on-login 一并修好）
- **已知遗留**:
  - 发送按钮 DOM 真正 enabled 的链路依赖 `liveData.messages` 反应式刷新 → 看到 typed text → `inputMessageContent.value.text` 非空 → `inputEmpty = false`。本 spec 不断言 DOM enabled（IDB roundtrip 已证 chain resolved），DOM 层面 enable 看起来是 Bug 5 的子问题（observeWithDeps liveData 反应式更新延迟），由 Round 2 单独跟进。

---

## Bug 2：退出登录不清本地 DB，残留旧数据 + 401 报错

- **状态**: ✅ 已修复
- **现象**：点击退出登录后，左侧工作区列表、工具列表等 UI 仍保留登录态时的数据，点击进入会因为 token 失效报 `401`，页面加载不出来。
- **期望行为**：退出登录应当清空本地 IndexedDB（`workspaces / dialogs / messages / providers / installedPluginsV2 / reactives ...`），恢复到全新无痕窗口首次打开时的默认空状态。
- **影响**：① 残留数据对下一个登录的账号是污染（甚至可能跨账号泄漏）；② UI 与后端鉴权状态不一致，全是 401 死链。

### 修复记录
- **日期**: 2026-05-04
- **根因**:
  1. `authSource.logout()` 仅清 token，不清 IDB → 10 张 server-routed 表的旧账号行残留 → liveQuery 让 UI 仍显示旧数据 → 点击触发 API → 401。
  2. 同 tab 跨账号场景额外踩到 `GET /api/v1/bootstrap` 的 `Cache-Control: private, max-age=10` 浏览器缓存：缓存键不含 `Authorization` header → A 登出后 10s 内 B 登录 → 浏览器返回 A 的 bootstrap 缓存 → IDB 里出现 A 的 row + 真正的跨账号泄漏。
- **核心改动**:
  - `src/data/local-cache.ts`（新建）：`clearAllSyncedTables()` 并行 `Promise.all` clear 10 张表
  - `src/data/auth.backend.ts:263-269`：logout 顺序 → emit 'logout' → await clearAllSyncedTables → clearAuth
  - `src-backend/data/routers/bootstrap.py:289-300`：bootstrap 响应额外加 `Vary: Authorization` header（Bug 2 同源 — 同 tab 跨账号 cache leak fix）
  - 10 张 `<table>.server.ts`（providers / workspaces / dialogs / messages / items / artifacts / assistants / installedPlugins / avatarImages / reactives）：listener `() => { unsubscribe realtime; reset lastVersion=0; reset inflight=null; (有 scopedPull 的) scopedPull.reset() }`
- **判据映射**:
  - 登出后所有 10 张同步表 `count() === 0` + sessionStorage `aiaw.bootstrap.*` flag 已清 → spec: `tests/e2e/bugs/bug2-logout-clears-idb.spec.ts::bug2 case1 [providers-rest|realtime-ws] logout clears 10 tables + storage flags`
  - 跨账号：A 登出 → B 登录 → 不出现 401 死链 + B workspace 立即出 + IDB 无 A residue → spec: `tests/e2e/bugs/bug2-logout-clears-idb.spec.ts::bug2 case2 [providers-rest] cross-account: A login → logout → B login same-tab → no A residue + no 401 死链`
  - listener idempotent（双 logout 不抛） → spec: `tests/e2e/bugs/bug2-logout-clears-idb.spec.ts::bug2 case3 [providers-rest] listener idempotent: double logout flow does not throw`
  - 后端 cross-account / Vary / unauth / #user-perfs 契约 → api: `tests/api/test_bug_auth_hydration.py::test_bootstrap_returns_only_callers_workspaces / test_bootstrap_response_carries_vary_authorization / test_bootstrap_unauth_rejected / test_bootstrap_returns_user_perfs_reactive_for_bug4`
- **故障注入红测**: 注释 `src/data/auth.backend.ts:264-268` 的 `await clearAllSyncedTables()` → bug2 case1 红 ✅（`table workspaces must be empty after logout (was 2 → 2)`）→ 恢复 → 绿 ✅
- **关联 bug 顺带消除**: 无（Bug 2 路径独立于 Bug 1/3/4 的 emit 'login'，但共享同一 auth-events bus）
- **已知遗留**:
  - 跨账号 case 中允许 `logging-out-A` 阶段的 `/reactives/...` 401（persistentReactive useLiveQuery 在 token clear 与 logout cleanup 之间的窗口期），仅 `logging-in-B` / `verify-B` 阶段 401 budget 为 0。这些 401 不构成用户可见的"死链"。

---

## Bug 3：登录后工作区列表不立即刷新，需要交互一下才显示

- **状态**: ✅ 已修复（随 Bug 1 顺带）
- **现象**：从干净的本地状态（无痕 / 新窗口）登录测试账号后，左侧工作区栏没有立刻把云端数据拉下来渲染；必须在页面上点一下、切一下路由之后，工作区才"延迟"显示出来。
- **期望行为**：登录成功后立刻触发一次拉取并渲染，无需用户交互。
- **疑似原因方向**：登录成功事件没有主动触发 store / liveQuery 刷新，依赖了某次后续路由切换才订阅到数据。

### 修复记录
- **日期**: 2026-05-04
- **根因**: 与 Bug 1 同源 — 登录后 router beforeEach bootstrap guard 不重跑（once-per-session sessionStorage flag）；workspaces.server.ts 的 observeList 已挂 dexie liveQuery 但 IDB 还是空（observeList 不主动 pull）。"切路由后才出现"= 那次路由变化触发 router.beforeEach → bootstrap → 写 IDB → liveQuery fire。
- **核心改动**: 见 Bug 1 修复记录（auth-events bus + applyTokenPair emit 'login' + router triggerBootstrapNow listener + 10 张 server.ts auth-events listener）
- **判据映射**:
  - 登录后无需路由切换，左侧 workspace nav 立即出云端 workspace（断言 store + IDB） → spec: `tests/e2e/bugs/bug3-workspace-list-after-login.spec.ts::bug3 [providers-rest|realtime-ws] login via UI → workspace appears in store within 3s, no route change`
- **故障注入红测**: 与 Bug 1 共享（注释 emit 'login' → bug1+bug3 spec 同时红）
- **关联 bug 顺带消除**: 随 Bug 1 一并消除

---

## Bug 4：登录后设置页默认模型 / 默认服务商为空，刷新才正常

- **状态**: ✅ 已修复（随 Bug 1 顺带）
- **现象**：登录后进入"设置"页，"默认模型"和"默认服务商"两项显示为空（即无痕窗口未登录时的默认值）。
- **临时绕过**：F5 刷新页面后，所有设置项被正确加载。
- **关联**：与 Bug 1、Bug 3 同源——首屏登录态下 user-perfs / providers 等 store 没有把云端数据拉到位，刷新一次后才完整 hydrate。
- **影响**：用户以为账号设置丢失，且也间接卡住 Bug 1（发送按钮判断需要默认模型）。

### 修复记录
- **日期**: 2026-05-04
- **根因**: 与 Bug 1/3 同源 — bootstrap 不在登录后重跑 → reactives 表的 `#user-perfs` 不被拉取 → persistentReactive 的 `useLiveQuery(get('#user-perfs'))` 走 dexie 缓存命中默认值 → 默认 provider=null + 默认 model=fallback gpt-5.1。F5 = 新 sessionStorage → bootstrap 重跑 → 命中后端 user_perfs。
- **核心改动**: 见 Bug 1 修复记录
- **判据映射**:
  - 登录后无需 reload，/settings 默认模型 + 默认服务商立即显示后端值（断言 dexie + Pinia）→ spec: `tests/e2e/bugs/bug4-default-model-after-login.spec.ts::bug4 [providers-rest|realtime-ws] login via UI → /settings sees server provider+model within 3s, no reload`
- **故障注入红测**: 与 Bug 1 共享
- **关联 bug 顺带消除**: 随 Bug 1 一并消除

---

## Bug 5：消息发送后进度条空转，根本没向 LLM 提供商发请求

- **状态**: ❌ 未修复
- **现象**：刷新页面、发送按钮恢复正常之后，输入消息点发送，UI 上出现"正在加载"的进度条；但打开浏览器开发者工具的 Network 面板，**根本没有任何指向 API 提供商的网络请求**。
- **结果**：进度条无限转，不会出回答；再次刷新页面也无效（请求始终没发出去）。
- **影响**：核心对话功能完全不可用。
- **关联（Round 1 测试期发现）**: Bug 1 修复后看到一个相邻症状 — DialogView 的 `inputMessageContent` 在 typed text 写进 IDB 之后，computed 没有立即 re-evaluate 出新值（liveData.messages 反应式更新延迟），导致发送按钮 DOM 仍 disabled。这一并入 Bug 5 的"send pipeline 反应式问题"调查范围，由 Round 2 单独诊断。
