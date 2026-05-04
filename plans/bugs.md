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
  - 发送按钮 DOM 真正 enabled 的链路依赖 `liveData.messages` 反应式刷新 → 看到 typed text → `inputMessageContent.value.text` 非空 → `inputEmpty = false`。本 spec 不断言 DOM enabled（IDB roundtrip 已证 chain resolved），DOM 层面 enable 是**独立 RTT 反应式延迟**问题（observeWithDeps liveQuery 异步 emit 与 Vue computed 重新评估之间的窗口）—— 2026-05-04 Bug 5 Round 3 修复后实测验证：临时给本 spec 加 `await expect.poll(sendBtn DOM enabled within 5s).toBe('enabled')` 探测，realtime-ws + providers-rest 双红（仍 disabled）→ 证明与 Bug 5 send-pipeline race fix 无直接关联，留待后续 reactivity-glue 调研。

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

- **状态**: ✅ 已修复
- **现象**：刷新页面、发送按钮恢复正常之后，输入消息点发送，UI 上出现"正在加载"的进度条；但打开浏览器开发者工具的 Network 面板，**根本没有任何指向 API 提供商的网络请求**。
- **结果**：进度条无限转，不会出回答；再次刷新页面也无效（请求始终没发出去）。
- **影响**：核心对话功能完全不可用。
- **关联（Round 1 测试期发现）**: Bug 1 修复后看到一个相邻症状 — DialogView 的 `inputMessageContent` 在 typed text 写进 IDB 之后，computed 没有立即 re-evaluate 出新值（liveData.messages 反应式更新延迟），导致发送按钮 DOM 仍 disabled。这一并入 Bug 5 的"send pipeline 反应式问题"调查范围，由 Round 2 单独诊断。

### 修复记录
- **日期**: 2026-05-04
- **根因（双段）**:
  1. **PrematureCommitError 路径（Round 2）**: DialogView 内 6 处 + create-dialog / workspace-actions / plugins / DialogList 内共 13 处 `runTx(['dialogs','messages',...], async () => {...})` 把 server-routed 表的写入包进 Dexie transaction。server-routed 仓库的每个写入内部 `await http.put(...)` —— Dexie transaction 一旦 await 一个非 Dexie promise 就自动 commit / abort 并在下一个 op 抛 `PrematureCommitError`。`stream()` 内 2× `appendMessage`（assistant + 新 inputing user）就在这个 runTx 里 → 第二次 appendMessage 抛 → `stream()` 抛 → outer `send()` 没 try/catch → unhandled rejection → `streamText(params)` 永远到不了 → mock LLM 0 hit → 进度条空转。
  2. **watcher fire-and-forget race 路径（Round 3，realtime-ws profile only）**: 拆 runTx 后裸露一个 pre-existing race。`appendMessage` 的第 1 次 `await repos.messages.add(...)` → cache.put → `liveData.messages.length` 变化 → DialogView 的 `watch([() => liveData.value.messages.length, ...])` fire → `updateChain` 内 `repos.dialogs.update(...)` **不 await**（fire-and-forget）用 cache 中**旧**的 dialog 重新 PUT 一份。同时 `appendMessage` 自己的 `await repos.dialogs.update(...)` 也在 fly。两个并发 PUT 到 server，server LWW 后到的赢；watcher 的版本 LACKS 第 1 次新加的 `msgTree[id]` 键（因为 watcher 用 cache.get 时的快照）→ ws push 把 LWW winner 推回 cache → cache.dialog.msgTree 失去 id 键。第 2 次 `appendMessage` 内 `[...d.msgTree[target], mt]` → spread of undefined → 抛 `TypeError: Xt is not iterable` → `stream()` 抛 → 同样 streamText 0 hit。realtime-ws 5/5 复现 / providers-rest 0/5（无 ws push 重写）。
- **核心改动**:
  - **Round 2（13 处 runTx 拆解 → sequential await）**:
    - `src/views/DialogView.vue:593+ / 634+ / 662+ / 916+ / 987+ / 1182+`：6 处函数（edit / deleteBranch / appendMessage 调用点 / 其他）的 runTx 拆为 sequential `await`，注释挂 `Bug 5 fix — sequential awaits, not a Dexie transaction`
    - `src/composables/create-dialog.ts`：`runTx` 拆解（创建 dialog + 创建 inputing message + dialog update msgTree 的三步顺序 await）
    - `src/composables/workspace-actions.ts`：workspace 删除 cascade 的 runTx 拆解
    - `src/stores/plugins.ts`：plugin 安装 / 卸载 / 修改的 runTx 拆解
    - `src/components/DialogList.vue`：dialog 删除 / 重命名的 runTx 拆解
  - **Round 3（DialogView race fix）**:
    - `src/views/DialogView.vue:1166`：新增 `const streamActive = ref(false)` sentinel
    - `src/views/DialogView.vue:1167-1408`：`stream()` 函数体外层包 `try { ... } finally { streamActive.value = false }`，stream 启动顶部置 `streamActive.value = true`
    - `src/views/DialogView.vue:544 / 575`：`updateChain` 与 `watch handler` 改 `async`
    - `src/views/DialogView.vue:564`：`updateChain` 顶部 `if (streamActive.value) return` noop guard
    - `src/views/DialogView.vue:573`：watcher PUT 路径 `await repos.dialogs.update(...)`（不再 fire-and-forget）
- **判据映射**:
  - bug5 spec 在 `realtime-ws` profile 5 次重复跑 5/5 绿（mock LLM hit ≥ 1 + assistant message 收敛 status='default' + 无 PrematureCommitError + 无 `TypeError ... is not iterable`） → spec: `tests/e2e/bugs/bug5-message-send-actually-fires-llm-request.spec.ts::bug5 [realtime-ws] login → input → send → LLM endpoint hit + progress finishes + assistant message rendered`
  - bug5 spec 在 `providers-rest` profile 5 次重复跑 5/5 绿（同上） → spec: `tests/e2e/bugs/bug5-message-send-actually-fires-llm-request.spec.ts::bug5 [providers-rest] login → input → send → LLM endpoint hit + progress finishes + assistant message rendered`
  - 现有 Bug 1-4 spec 全 2/2 绿无 regression（providers-rest 44 passed + realtime-ws 47 passed e2e 全集 + 290 pytest 全集）
- **故障注入红测（双轴）**:
  - **A. 主 race 复现（验 Round 3）**: 注释 `src/views/DialogView.vue:564` 的 `if (streamActive.value) return` → `rm -rf tests/.builds/realtime-ws` → bug5 spec realtime-ws 红 ✅（`TypeError: kn is not iterable` + mock LLM hit=0 + `allFakeRequests=[]`）→ 恢复 → 绿 ✅
  - **B. PrematureCommit 复现（验 Round 2）**: 把 `stream()` 内 2× appendMessage 用 `await runTx(['dialogs','messages'], async () => {...})` 包回去（同时 `import { runTx } from 'src/data'`）→ `rm -rf tests/.builds/providers-rest` → bug5 spec providers-rest 红 ✅（mock LLM hit=0 + `allFakeRequests=[]` + 单字符 `[error] F` —— PrematureCommitError 在 minified bundle 下打印被截断，spec sentinel 用 hit count 不依赖字符串）→ 恢复 → 绿 ✅
- **关联 bug 顺带消除验证**:
  - **Bug 1 已知遗留（DOM send button enabled）**: 临时给 bug1 spec 末尾加 `await expect.poll(sendBtn enabled within 5s).toBe('enabled')` 探测 → realtime-ws + providers-rest 双红（"Received: disabled"）→ 证明 Bug 1 已知遗留是**独立 RTT 反应式延迟**问题（observeWithDeps liveQuery 异步 emit 与 Vue computed 重新评估之间的窗口），与 Bug 5 Round 3 race fix 无直接关联。Bug 1 spec 维持原样不升级 DOM 断言（保留 IDB roundtrip 作为 Bug 1 修复信号），Bug 1 段「已知遗留」描述同步更新（已删去"由 Round 2 单独跟进"措辞，改为"独立 RTT 问题，留待后续观察"）。
- **修复 commit**: `fix(dialog-tx): 拆掉 server-routed dialogs/messages 的 runTx 包装并修 stream 期间的 dialog write race`
- **已知遗留**:
  - Bug 1 已知遗留（DOM enabled 反应式延迟）独立未消除，留待后续 reactivity-glue 调研
  - bug5 spec 跑过程中观察到一次 `[page-error] TypeError: Cannot read properties of null (reading 'specificationVersion')` —— 从 ai SDK provider 解析路径出（minified `h1 → jb → Ce`），不影响 mock hit + assistant message 收敛，spec 不 fail，记录待观察（疑与 streamFlush 收尾 / 同 tab 第二次 send 的某个 stale provider ref 有关）
  - **Bug 6（性能延伸）**: Round 2 拆 runTx 后，server-routed 表写入退化为「裸 await 串行 HTTP」—— 单次 `appendMessage` = 2 串行 PUT（~400ms），单次 `stream()` 在 streamText 起跑前要打 4 串行 PUT（~800ms）。用户感知发送 / 编辑按钮有 0.5-1s 延迟。已立 Bug 6 跟进。

---

## Bug 6：对话页发送 / 编辑分支按钮延迟 0.5-1s（串行 PUT × 3-4 次）

- **状态**: ❌ 未修复
- **现象**：在对话页输入消息后点「发送」或点消息项的「编辑」（创建分支编辑）按钮，UI 响应有半秒到一秒的明显延迟。打开 Network 面板观察到点击瞬间发出 3-4 个串行 PUT，每个 ~200ms。
- **触发场景**：每次点击 send / edit / 创建分支 / saveItems。每次 send 起码 4 次串行 PUT（2× messages.add + 2× dialogs.update，分散在两次 `appendMessage` 调用里）；edit 是 2× appendMessage PUT + N 次 saveItems items.bulkPut。
- **影响**：核心交互响应速度差，长会话累积越用越觉得「卡」。非阻塞功能正确性，纯 UX 问题。
- **关联**: Bug 5 Round 2 修复（commit `5769ec6`，拆 runTx → sequential await）的性能负债。原 runTx 仍是 4 次 PUT，但 Dexie 同步路径靠 IDB tx 把多个 cache.put 合并成一个 microtask；现在每次 PUT 串行 await HTTP，cache.put 也只能在 HTTP 返回后才执行（`putOne` 形态：`http.put → cache.put(decoded)`），UI liveQuery 一直要等到所有 PUT 完成才开始 emit 新行 → 用户感知延迟 = 全部 PUT RTT 之和。

### 修复方案（已与用户对齐：方案 1+3）

- **方案 1**：`DialogView.vue::appendMessage` 内 2 次 PUT 并行（dialog cache 读是同步的，msgTree 计算是纯函数；server 侧 dialogs/messages 之间无外键，先后顺序无所谓）。`Promise.all([repos.messages.add, repos.dialogs.update])` 把 2 次 RTT 折叠成 1 次。
- **方案 3**：所有 server-routed `<table>.server.ts` 的 `putOne` 改成「local-first cache write」—— 先 `db.<table>.put(value)` 让 liveQuery 立刻 emit，然后再 await HTTP，HTTP 返回后用 server canonical version 重新 put 一次（覆盖 server 侧规整的字段如 updated_at）。caller 仍然 await 整个 putOne，但 UI 在 HTTP 等待期已经看到新行（liveQuery emit 在 HTTP 返回前）。
- **不做的方案 2**：`stream()` 把 2× appendMessage 进一步合成 1 次 Promise.all。原因：第 2 次 appendMessage 用第 1 次的 id 作 parent，dialog.msgTree 合并需要拍平到一次 dialog write，逻辑改动较深，方案 1+3 已足够消除用户主要痛点。
- **不做的方案 4**：后端聚合 endpoint。需要 plan + spec + 后端 router 改动，工程量大，留作未来优化。

### 修复记录
- **日期**: 2026-05-04
- **核心改动**:
  - `src/data/repositories/<10 tables>.server.ts::putOne`：每张表 putOne 改成「local-first」——`db.<table>.put(value)` 先于 `http.put`，HTTP 返回再 cache.put(decoded) 做 server canonical 覆盖。10 张全部改：messages / dialogs / items / artifacts / workspaces / assistants / providers / installed-plugins / avatar-images / reactives。一致性优先，未来加新表沿用同模板。
  - `src/views/DialogView.vue::appendMessage`：dialog cache.get + msgTree 计算前移到 HTTP 之前；2 次 server-routed PUT 用 `Promise.all([repos.messages.add, repos.dialogs.update])` 并行触发，单次 appendMessage 的 RTT 从 ~400ms 降到 ~200ms。
- **判据映射**:
  - 点 send 后 IDB messages 表新增的 2 行（assistant placeholder + 新 inputing user）必须在 HTTP_DELAY=1500ms 以内的 300ms 窗口内出现 → spec: `tests/e2e/bugs/bug6-dialog-write-optimistic.spec.ts::bug6 [providers-rest|realtime-ws] click send → IDB shows new branches before HTTP completes`
  - 现有 bug5 spec 在 providers-rest + realtime-ws 双 profile 5/5 仍稳定绿（mock LLM hit ≥1 + assistant message 收敛 status='default' + 无 PrematureCommitError）→ regression check
- **故障注入红测**: 把 `messages.server.ts::putOne` 内 `await db.messages.put(value)` 去掉（恢复 HTTP-first） → `rm -rf tests/.builds/providers-rest tests/.builds/realtime-ws` → bug6 spec 双 profile 红（IDB messages count 在 300ms 内 < 3）→ 恢复 → 绿
- **预估收益**:
  - 单次 send 点击 → streamText 起跑前的 RTT：从 4 串行 PUT (~800ms) → 2 串行 appendMessage × 1 并行 PUT (~400ms)
  - 单次 send 点击 → UI 看到分支：从 ~800ms（必须等所有 HTTP 完成）→ ~50ms（local cache 已 put）
  - 单次 edit 点击 → UI 看到新分支：从 ~400ms → ~50ms（appendMessage 部分）
- **已知遗留**: items.bulkPut 在 saveItems 仍是串行 N 次 PUT（受 `(user_id, sha256) blob_refs` 唯一约束需要串行）；edit 带 N 个 attachment 的场景仍有 N×200ms 延迟，但 N 一般 ≤ 3，且这部分在 saveItems 而不在视觉上需要 0ms 响应的 appendMessage 路径里，先不动。
