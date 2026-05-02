# 云同步架构分析 + 服务端优先迁移方案

## Context

当前仓库 (AIaW fork) 的云同步基于 **Dexie + dexie-cloud-addon**（本地优先 / IndexedDB → 官方托管 SaaS `https://znm3rqzc8.dexie.cloud`）。后端 `src-backend/app.py` **不参与同步**，只做 CORS 代理与文档解析。业务代码 32 处直接 `import { db }` 调 `db.<table>.*`，没有数据访问层抽象，登录/账户/订阅 UI 直接耦合 `db.cloud.*` API。

目标是迁移到 **服务端优先**：自己的 FastAPI 承担权威存储，客户端通过 REST + WebSocket 读写，IndexedDB 退化为缓存。本计划重点在「**每一阶段都能独立合并、构建、上线、端到端验证**」，不需要全部改完才能跑通。

部署形态目标：**多用户 SaaS-style**（不仅仅是单人自部署），所以鉴权必须从一开始就支持多账号、可对外开放注册或受控开放。

---

## 修订记录

- **2026-05-02 · Stage 2 / Step 5 落地**
  - **背景**：Step 4 完成后客户端只有 WS 一种 transport。Step 5 加 SSE / poll 降级路径 + auto 自动选档，让代理 / NAT / 公司网络拒 WS upgrade 时仍能走最低保证最终一致。spec 在 Phase 6 落地（5 个 pytest case + 4 个 playwright case），代码缺位时 case2 poll / case3 / case4 spec-first 红（5 个 SSE pytest 全 404）。
  - **变更**：
    - 后端 `src-backend/data/routers/sse.py`：`GET /api/v1/stream/sse?since=&tables=...`，`text/event-stream`，鉴权走 `Authorization: Bearer <jwt>`，每帧带 `id:<rev>`，`Last-Event-ID` 头作为 `since` 兜底。复用 `realtime.broker` + `Subscription` 与 WS 同构；keepalive 用 SSE comment（`: keepalive`）每 25s 一发；未知表降级为 `event: error` 帧而非 close。`app.py` 把 `sse` 路由加进 `_enable_backend_data_api()` flag 守卫的挂载列表（与 stream / providers 同款 lazy import）。
    - 前端 transport 抽象：新增 `src/data/realtime-types.ts`（`TransportConn` 接口 + `RealtimeEvent` / `TransportName` 类型），`src/data/realtime-ws.ts` 重构为 `WsTransport` 类（保留 `RealtimeState` 导出 + `maxReconnectAttempts` 选项让 auto-router 能限攻击），`src/data/realtime-sse.ts` 实现 `SseTransport`（用 `eventsource@3` 包注入 `customFetch` 让 `Authorization` 头能加上），`src/data/realtime-poll.ts` 实现 `PollTransport`（5s `setInterval` 调 Stage 1 已有的 `GET /api/v1/<table>?since=`，`row` 字段保持 `ProviderRow` envelope 与 WS / SSE 同款契约）。
    - 新增 `src/data/realtime.ts` 作为 dispatcher：按 `RealtimeTransport` 选 `'ws' | 'sse' | 'poll' | 'auto' | ''(NullTransport)`；`AutoTransport` 用每个 transport 的 `ready(timeoutMs)` 做就绪探针（WS 3s、SSE 5s、poll 0s 即时），失败触发 `demote()`：unsubscribe 旧 transport 全部 listener → switch → 用 carryover 的 `lastRev` 在新 transport 上重 subscribe。`realtime.subscribe(table, onEvent, since?)` 是统一入口，`window.aiawRealtime.transport` 暴露当前档位（`'ws' | 'sse' | 'poll'`）供 e2e 校验；`watch(authSource.user)` 仍在本文件 hook（从 realtime-ws.ts 迁过来），冷启动 / 重新登录时叫醒底层 transport。
    - `providers.server.ts`：import 改为 `from '../realtime'`；`ensureRealtimeSubscription()` 守卫从 `RealtimeTransport !== 'ws'` 放宽到 `!RealtimeTransport`（任何非空 transport 都启用），SSE / poll / auto 走同一份 `db.providers.put(e.row.data)` 解包契约。
    - `src/data/index.ts`：`realtime` / `createRemoteSyncSource` / `RealtimeEvent` / `RealtimeState` 导出口从 `./realtime-ws` 改到 `./realtime`。
    - 脚手架增量：`playwright.config.ts` 加 3 profile（`realtime-sse:9012` / `realtime-poll:9013` / `realtime-auto:9014`）；`tests/env/.env.test.realtime-{sse,poll,auto}` 三份；`tests/scripts/run-playwright.sh` 加 3 个 build-profile 步骤；`tests/scripts/backend-start.sh` `CORS_ALLOW_ORIGINS` 扩 9012 / 9013 / 9014（`localhost` + `127.0.0.1` 各一份）；`tests/e2e/helpers/net.ts` 新增 `blockSSE(context)` + 重写 `blockWS(context)` 用 Playwright 1.48+ 的 `routeWebSocket()`（旧实现用 `context.route('**/*')` 只覆盖 HTTP 不覆盖 WS upgrade，等同失效）。
    - 测试：`tests/api/test_realtime_sse.py` 5 case（unauth 401 / invalid token 401 / replay then live / account isolation / Last-Event-ID resumes from rev）全绿；`tests/e2e/stage2/step5-transport-degradation.spec.ts` 4 case 全绿（case1 sse 2.0s / case2 poll 6.9s / case3 auto→sse 3.5s / case4 auto→poll 9.9s）。
  - **影响范围**：
    - `pnpm test:api` 33 → 38 全绿（+5 SSE case）；`pnpm test:e2e` 17 / 102（85 skipped 是 profile gate）全绿。
    - `tests/README.md` §3 判据映射表加 Stage 2 / Step 5 行；§5 helper ↔ plan 词汇表加 `blockSSE`；§4 已知预期红仍空（spec-first 阶段的 5 SSE pytest red 已转绿）；§2 端口表新增 9012 / 9013 / 9014 三 profile。
    - my-deploy 默认 `REALTIME_TRANSPORT=` 空（NullTransport），行为字节级等同 Stage 1 收尾；Northflank 控制台开 `REALTIME_TRANSPORT=auto` 即灰度启用全功能；`ws` / `sse` / `poll` 三个固定档也支持。
  - **避坑（落地踩过 + 修了的）**：
    - ① **PollTransport 的 `row` 字段最初传了 `row.data`**（unwrap），导致 providers.server.ts 的 `e.row && e.row.data` 解包条件 false → cache 永不 put → spec 报 "B did not converge ... last B rows: []"。修为传 `row` 完整 `ProviderRow` envelope，与 WS / SSE 同款。教训：每加新 transport 都要走「`row` = envelope，`row.data` = 业务对象」契约，否则 cache 形状会沉默错位（参考 CLAUDE.md 已有的相同警告）。
    - ② **`blockWS` helper 旧实现用 `context.route('**/*')` 不能拦 WS upgrade**：Playwright 的 `route()` 只覆盖 HTTP/fetch/XHR，WebSocket 走另一通道，URL 也不是 `ws://...`（浏览器拿到的是 HTTP Upgrade 请求）。改用 Playwright 1.48+ 的 `context.routeWebSocket('**/api/v1/stream**', ws => ws.close({code: 1008}))`，case3 / case4 才能真把 WS 拒掉触发 fallback。
    - ③ **build cache key 含 git rev 但不含工作树 diff**：每次源代码改动后没 commit 直接 `pnpm test:e2e`，cache 命中旧产物，bug 修了 spec 还是红。Step 5 需要 `rm -rf tests/.builds/<profile>` 才能强制 rebuild；连续修 + 跑红 → 修 + 跑绿的循环里这一步不能省（CLAUDE.md 已有警告但没 commit 习惯时仍会踩）。
- **2026-05-02 · Stage 2 / Step 4 落地**
  - **背景**：Phase 5 在 Step 4 代码缺位时已将 4 条通过判据沉淀为 `tests/e2e/stage2/step4-providers-realtime.spec.ts`（spec-first：case1 / case2 在 realtime-ws profile 下 spec-first 红、case3 / case4 绿）。本次按 plan 把 `providers.server.ts.observeList()` 接到 `RemoteSyncSource`，并把 `REALTIME_TRANSPORT` env 暴露到 `src/utils/config.ts`。
  - **变更**：
    - `src/utils/config.ts`：新增 `RealtimeTransport`（trim 后字符串），Step 4 仅识别 `'ws'`，`'sse' / 'poll' / 'auto'` 留给 Step 5。
    - `src/data/repositories/providers.server.ts`：模块级 `realtimeUnsubscribe` + `ensureRealtimeSubscription()`，第一次 `observeList / observeFind / observeOne` 调用时 lazy 启动 `realtime.subscribe<ProviderRow>('providers', ...)`；handler 把 server 的 `ProviderRow` envelope 解包到 `e.row.data` 写回 `db.providers` cache（`pull()` 同款解包契约），同步 `lastVersion`。订阅 idempotent，整个页面生命期一份。
    - flag 守卫：`RealtimeTransport !== 'ws'` 时 `ensureRealtimeSubscription()` early return → providers-rest profile 不会建 WS（保留 case3 期望）；baseline 走 dexie repo，Step 4 代码不被加载（保留 case4 字节级一致）。
  - **影响范围**：
    - 落地后 `pnpm test:e2e -g step4` 4 case 全绿（case1 1.6s / case2 31.5s 含 30s 离线窗 / case3 3.5s / case4 1.6s）；`pnpm test:api` 33 / 33 仍绿；整套 `pnpm test:e2e` 13 passed / 26 skipped / 0 failed。
    - `tests/README.md` §4「已知预期红」清空 step4 case1 / case2 行（预期红已转绿）。
    - my-deploy 默认 `REALTIME_TRANSPORT=` 为空，行为字节级等同 Stage 1 收尾；Northflank 控制台开 `REALTIME_TRANSPORT=ws` 灰度即可启用。
  - **避坑（落地踩过 + 修了的）**：
    - ① **server WS event 的 `row` 字段是完整 `ProviderRow` envelope，不是 unwrap 过的 `CustomProvider`**（`src-backend/data/routers/providers.py::_to_event` 写明了）。最初 handler 直接 `db.providers.put(e.row)` 让 B 端 IDB 存了 envelope，case1 expectRowSync 输出 A=unwrap / B=envelope 不一致。改为 `db.providers.put(e.row.data)` 与 `pull()` 同款解包契约后转绿。
    - ② **build cache key 含 git rev 但不含工作树 diff**：未提交的源代码改动 cache 命中旧产物，跑出来的 spec 仍然红。需要 `rm -rf tests/.builds/<profile>` 强制 rebuild，或先 commit 让 git rev 推进。Step 5 / 后续 Step 落地时同样需要注意。
- **2026-05-01 · 鉴权方案变更**
  - **背景**：原 Stage 1 假设可走「JWKS 桥接」——后端拉 Dexie Cloud 的 `/.well-known/jwks.json` 本地验签 token。实测 Dexie Cloud **未公开 JWKS、也无 token introspection 端点**（仅暴露 `/token` + `/sync`），桥接路径不可行。
  - **结合多用户目标**：「解码不验签 Dexie JWT」只能撑单用户多账号、对真实多用户场景是裸奔，不可接受。
  - **变更**：把 Stage 5 里的「切换鉴权」拆出来提前到 **Stage 1.5**，作为 Stage 1 与 Stage 2 之间的独立可发布步骤；Stage 5 收窄为「移除 dexie-cloud-addon + 客户端最终切换」。
  - **影响范围**：Stage 1 不再含 `auth.py`、不再有「鉴权桥接」风险项；Stage 5 不再含 auth 切换；新增 Stage 1.5；端到端验证表新增一行。
- **2026-05-01 · Stage 1 七步细化 + 一致性修订**
  - **变更**：Stage 1 内部 7 个 Step 显式写回 plan（Step 1 ✅ → Step 2 ✅ → ~~Step 3 JWKS 鉴权~~ 替换为 Stage 1.5 → Step 4 http.ts → Step 5 providers.server.ts → Step 6 Flag 路由 → Step 7 端到端验证）。Step 1-2 已完成；Stage 1 出口（Step 7 通过）即可按"上线但不启用"策略合并到 my-deploy。
  - **附带订正**：表数从 9 改为 10（活跃）+ 1（废弃 `canvases`）；Stage 1.5 router 列表补 `link-dexie`；User 模型字段补 `linked_dexie_email`；Stage 0 接口草稿与实际 `src/data/auth.ts` 的差异补注；现有用户起点补第三类（backend-first 新用户）。
- **2026-05-01 · 实际 env 名修订 + 后端条件挂载**
  - **背景**：plan 早期写的是 `VITE_DATA_TABLES` / `VITE_REALTIME_TRANSPORT`，但项目用 Quasar `process.env.*` 读法、不走 Vite `VITE_` 前缀；实际代码里 `src/utils/config.ts:15` 是 `BACKEND_DATA_TABLES`。统一改名。
  - **后端条件挂载**：原 plan 没考虑 Northflank 这类不开 backend data API 的部署场景。`src-backend/data/auth.py` 在 import 期间会因 `JWT_SECRET` 缺失抛 `RuntimeError`，导致 `app.py` 顶层 import 路由就让整个 FastAPI 崩（连原来的 `/cors`、`/doc-parse`、SPA 静态都起不来）。已在 commit `732aee0` 引入 `BACKEND_DATA_API_ENABLED` flag 条件挂载，与前端 `BACKEND_DATA_API_URL` 对称：两边都关 = Stage 0 行为；两边都开 = Stage 1.5 全功能。`.env.docker` 默认不含此 flag，已上线镜像走 Stage 0 行为。
  - **影响范围**：plan 内 3 处 `VITE_DATA_TABLES` 改名；新增「横切关注点 · 后端模块条件挂载」一条；Stage 1.5 后端依赖列表注明 import 期不应被无 flag 部署触达。
- **进度快照（2026-05-01）**
  - Stage 0 ✅ 已合并到 my-deploy 上线运行
  - Stage 1 / Step 1（后端骨架 + Postgres + `/api/v1/health`）✅ commit `e4d3210`
  - Stage 1 / Step 2（providers 表 + REST CRUD + `?since=` 增量）✅ commit `62318f6`
  - Stage 1 / ~~Step 3（JWKS 鉴权）~~ → 替换为 Stage 1.5（自家 JWT 多用户鉴权）✅ commits `47b6946` / `6ea48e9` / `d80638b` / `0720e10`
  - Stage 1 / Step 4（前端 HTTP 层）✅ commit `4ae19a3`
  - Stage 1 / Step 5（`providers.server.ts` REST 实现）✅ commit `a628e4b`
  - Stage 1 / Step 6（flag 路由按表选实现）✅ commit `b1586c9`
  - Stage 1 / Step 7（端到端验证）✅ 手测通过（双登录入口 + 跨设备同步 + flag 切换）
  - Stage 1 收尾 + Stage 1.5 已整体合并到 `my-deploy` 并 push（commit 区间 `2b26349..732aee0`，已通过 Northflank 自动部署）
  - 部署侧默认 flags 全关（`.env.docker` 未含 `BACKEND_DATA_API_URL` / `BACKEND_AUTH` / `BACKEND_DATA_API_ENABLED`），线上行为字节级等同 Stage 0；按需在 Northflank 控制台开 flag 灰度
  - 下一步：进入 **Stage 2**（实时订阅通道，6 Step 已细化写回 plan）。`link-dexie` 自动调用推迟到 Stage 3 第一张迁移表落地时一起补——届时 mapping key 真正被读到。
- **2026-05-02 · 已知问题 #2 修复 · server-routed 表走 `unsyncedTables`**
  - **背景**：Stage 2 / Step 3 验收时观察到「仅登 backend 账号未登 Dexie 时，新建/修改 provider 后 UI 列表不刷新」。读 dexie-cloud-addon 源码（`createIdGeneration` / `createImplicitPropSetter` / `createMutationTracking` 三个 dbcore middleware）确认：addon 默认把 schema 里所有非 `$` 前缀的表标 `markedForSync = true`，对这些表的 readwrite 事务会强制并入 `$<table>_mutations` mutation 表，并在每条 `add/put` 上注入 `owner`/`realmId`、读 `trans.currentUser` 上下文。backend-only 用户 `currentUser = UNAUTHORIZED_USER`，与 Stage 2 的 `providers` 写路径冲突。
  - **变更**：在 `src/utils/db.ts` 的 `db.cloud.configure(...)` 里按 `BACKEND_DATA_API_URL ∩ BACKEND_DATA_TABLES ∩ SERVER_CAPABLE_TABLES` 计算 `unsyncedTables`，把已切到后端的表（当前仅 `providers`）从 addon 的 `markedForSync` 摘掉。`SERVER_CAPABLE_TABLES` 抽到 `src/data/server-tables.ts` 共享模块（避免 db.ts ↔ repositories/index.ts 循环依赖）。
  - **影响范围**：flag 关时 `unsyncedTables = []`，行为字节级等同此前；flag 开时 `providers` 退化为普通 Dexie 本地表，`db.providers.put/toArray/liveQuery` 全部短路 addon middleware。Stage 3+ 每张新搬到后端的表自动通过 `BACKEND_DATA_TABLES` 加入此名单，无需再改 db.ts；只需在 `server-tables.ts` 把表名加进 `SERVER_CAPABLE_TABLES`。
- **2026-05-02 · 已知问题 #1 修复 · `'idle'` 状态加 user watcher**
  - **背景**：Stage 2 / Step 3 验收时识别出 `RealtimeConn` 进 `'idle'` 后无自动恢复机制（详见原「已知问题 #1」段）。本次 Step 3 收尾把这个边界 bug 一并修掉。
  - **变更**：`src/data/realtime-ws.ts` 加 `wakeIfIdle()` 公共方法 + 模块级 `watch(authSource.user)`：null→user 跳变时调 `wakeIfIdle()`，覆盖 (a) 冷启动 subscribe 早于登录、(b) 4001 + tryRefresh 失败后用户重新登录两条主路径。`reconnectAttempt` 在 wake 时置 0，避免被先前的退避计数拖慢首次连接。
  - **影响范围**：flag 全关部署里 watch 仍然注册并被 Dexie 登录态变化触发，但 `wakeIfIdle()` → `ensureConnected()` 因 `BackendApiBaseURL` 为空 early return，无网络副作用。flag 开时弥补 plan 标识的 #1 路径，让 Step 4 接 repo 后跨 tab 体验稳定。跨 tab refresh 轮换瞬间（prev 仍非 null）的窄边界仍未覆盖，留作未来观测——触发条件需要 storage 事件 + WS close 同时发生，概率极低。
- **2026-05-02 · Stage 2 Step 1-3 代码落地 + plan 入仓库**
  - **变更**：Stage 2 前 3 个 Step 的代码已全部合并到 `feature/change-cloud-sync-claude` 分支（尚未合 my-deploy）；plan 文件本身从 `~/.claude/plans/` 复制到仓库 `plans/cloud-sync-migration.md`，CLAUDE.md 加「长期迁移工作流」段并入库（详见进度快照 2026-05-02）。
  - **影响范围**：plan 维护规则正式生效——动态进度（当前 Step、commit hash、未解决问题）一律写本文件，不写 CLAUDE.md；任何改动需配套通过判据，每个 Step 完成后必须更新「进度快照」段；方案有调整必须先在「修订记录」追加条目再改正文。
  - **后续维护**：本文件即权威版；`~/.claude/plans/1-ethereal-lollipop.md` 仅给 Claude Code `/plan` 命令读，按需手动 `cp` 仓库版同步。
- **2026-05-02 · Stage 2 Step 5/6 通过判据回填脚手架引用 + 一致性修订**
  - **背景**：Phase 6（test-infrastructure plan）落地后已建立「每条通过判据 → 一条 spec/api 引用」的硬性约定；Stage 2 Step 4 已遵守，但 Step 5（SSE/poll 降级）与 Step 6（Stage 2 收官）原文写于 Phase 6 之前，3 / 6 条判据均无 spec/api 行；且 Step 5 引用的 `realtime-sse` / `realtime-poll` / `realtime-auto` profile 在当前 `playwright.config.ts` 中不存在，Step 6 列出的 6 个场景与 Step 1/3/4 已落 spec 大量重叠却未声明 reuse。
  - **变更**：
    - Step 5「做什么」段补「脚手架增量」小节（3 个新 profile + 9012/9013/9014 端口 + env 文件 + helper `blockSSE` 增量 + `CORS_ALLOW_ORIGINS` 扩端口 + `window.aiawRealtime.transport` 暴露），3 条「通过判据」每条补 `- spec:` / `- api:` 行，对应 `tests/e2e/stage2/step5-transport-degradation.spec.ts` 4 case 与 `tests/api/test_realtime_sse.py` 5 case
    - Step 6 重写为「回归判据（reuse 既有 spec / api，不重写）」+「上线把关判据（脚手架不覆盖，必须手测 + 写进度快照）」+「Stage 2 出口判据」三段：回归 5 类全部 reuse Step 1-5 已落 spec 路径；bundle 体积 / RSS soak / p95 延迟标注为非自动化、明确手测交付物（KB / MB / ms 数写进进度快照）；删去模糊措辞「6 场景全过 + 后端无内存泄漏（连续运行 1h 看 RSS 平稳）」改为可执行清单
    - 风险段每条补「对应自动化」一行，明确哪条由 spec 兜、哪条由 soak 兜、哪条暂无脚手架覆盖
  - **影响范围**：仅 plan 文档；不影响代码。Step 5 落地 PR 应同时新增 `playwright.config.ts` projects / `tests/env/.env.test.realtime-{sse,poll,auto}` / 上述 spec 文件，与 plan 此处描述一致；Step 6 落地 PR 不引入新 spec、只新增 `tests/scripts/soak.sh` 与一次性手测交付。test-infrastructure plan 无需同步更新——Phase 6 守则已涵盖「Step 落地同 PR 落 spec」。
- **进度快照（2026-05-02）**
  - **Stage 2 / Step 1**（后端 broker + WS `/api/v1/stream`）✅ commit `e6c5335`
  - **Stage 2 / Step 2**（前端 `SyncSource` 接口 + `DexieSyncSource` 重构）✅ commit `7755de7`
  - **Stage 2 / Step 3**（前端 `realtime-ws.ts` — `RealtimeConn` 单例 + `createRemoteSyncSource`）⚠️ **代码已合 commit `aa0f25b`，验收进行中**
    - 副 commit `e73e52e`（修复 AccountPage 主退出登录级联登出 Dexie 会话）— 联动测试时浮现的 Stage 1.5 时期遗留 bug，本次 Step 3 验收前一并修掉
    - 验收三个场景里**场景 A 部分通过**：Console `realtime.subscribe('providers', cb)` 能收到 event（WS 握手 / 子协议鉴权 / SQL replay / 派发都正常）
    - 验收**场景 B（主动断网重连）/ 场景 C（token 过期 close 4001 + refresh + 重连）尚未独立测试**
    - 详见下方 §Stage 2 / Step 3「当前状态」段
  - **plan 文件 + CLAUDE.md 入库** ✅ commit `f2a39d4`
  - **已知问题 #2 修复**（server-routed 表 `unsyncedTables`）✅ commit `b080005`
  - **已知问题 #1 修复**（`'idle'` 状态加 `authSource.user` watcher）✅ commit `b5bc422`
  - **本次会话补跑的端到端验证**（之前未独立测过）：
    - Stage 1.5 账号隔离 / refresh token 流转 / logout 吊销旧 refresh ✅
    - Stage 1 Step 2 soft-delete tombstone（DELETE 后 list 仍出 `deleted:true,data:null`） ✅
    - Stage 2 Step 1 WS 账号隔离（A 订阅时 B 的 PUT 不进 A 频道） ✅
    - Stage 2 Step 1 心跳超时（25s ping + 10s 无 pong → 35s server close） ✅
  - **Stage 2 / Step 4**（`providers.server.ts.observeList()` 接 `RemoteSyncSource` + `RealtimeTransport=ws` 守卫）✅ — `pnpm test:e2e -g step4` 4 case 全绿（case1 1.6s / case2 31.5s / case3 3.5s / case4 1.6s）；详见修订记录 2026-05-02 · Stage 2 / Step 4 落地
  - **Stage 2 / Step 5**（SSE / poll 降级路径 + auto 自动选档）✅ — `pnpm test:api` 33 → 38 全绿（+5 SSE case）；`pnpm test:e2e` 17 / 102（85 skipped 是 profile gate）全绿（step5 case1 sse 2.0s / case2 poll 6.9s / case3 auto→sse 3.5s / case4 auto→poll 9.9s）；故障注入红测：注掉 PollTransport 派发循环 → case2 报 "B did not converge ... last B rows: []" 红信号充足，恢复后转绿。详见修订记录 2026-05-02 · Stage 2 / Step 5 落地
  - 下一步：进 Step 6（Stage 2 收官），按 plan 不引入新 spec、只 reuse Step 1-5 全套作为回归判据 + 写「上线把关」3 个手测交付物（bundle KB 数 / RSS 起止 MB 数 / p95 ms 数）进进度快照，然后翻 Stage 2 上线 flag 合 my-deploy。

---

## Part 1 — 现状架构分析

### 1.1 同步相关文件（按职能分类）

**前端 — 同步基础设施**
- `src/utils/db.ts` — Dexie schema (v6, 10 张活跃表 + 1 张废弃 `canvases`) + `db.cloud.configure({ databaseUrl: DexieDBURL, requireAuth: false, customLoginGui: true, nameSuffix: false })`，仅当 `DexieDBURL` 非空时才挂载 `dexieCloud` addon
- `src/utils/config.ts` — 暴露 `DexieDBURL` / `LitellmBaseURL` / `BudgetBaseURL` 等同步/账户/计费相关字段
- `src/composables/live-query.ts` — `useLiveQuery` / `useLiveQueryWithDeps`（基于 `dexie/liveQuery` + `useObservable`）
- `src/composables/persistent-reactive.ts` — 用 `db.reactives` + liveQuery 实现的持久化响应式对象
- `src/composables/local-reactive.ts` — LocalStorage 版（不走云）
- `src/composables/sync-ref.ts` — 双向同步 ref

**前端 — 同步状态/账户 UI**（直接依赖 `db.cloud.*`）
- `src/composables/login-dialogs.ts`（`db.cloud.userInteraction` + `db.cloud.currentUser`）
- `src/composables/first-visit.ts`（`db.cloud.login()`）
- `src/composables/subscription-notify.ts`、`src/composables/get-model.ts`
- `src/components/AccountBtn.vue`、`src/pages/AccountPage.vue`（`login/logout/sync` 全在这里）、`src/pages/ModelPricing.vue`
- `src/router/routes.ts:80-85` — 根据 `DexieDBURL` 是否存在条件性注册 `/account`、`/model-pricing`

**前端 — 业务侧直读直写 db 的位置**（耦合点）
- Pinia stores: `src/stores/{workspaces,assistants,providers,plugins,user-data}.ts`
- Composables: `src/composables/{create-dialog,create-artifact,close-artifact,workspace-actions}.ts`
- View: `src/views/DialogView.vue` 大量 inline `db.<table>.*` + `db.transaction()`

**后端**
- `src-backend/app.py` — 仅 `/cors/proxy`、`/doc-parse/parse`、`/searxng`、`/`（静态）。**不涉及同步**。
- 同步走 Dexie 官方 SaaS，仓库未自部署同步服务器；`package.json`: `dexie@^4.0.11` + `dexie-cloud-addon@^4.0.11`。

### 1.2 数据流

```
UI 组件 ──→ Pinia store / composable ──→ db.<table>.add/put/update/delete
                                              │
                                              ▼
                              IndexedDB (Dexie, schema v6)
                                              │
                              dexie-cloud-addon (后台增量推送)
                                              │
                                              ▼
                                  https://znm3rqzc8.dexie.cloud (官方托管)
                                              │
                              dexie-cloud-addon (拉取变更)
                                              │
                                              ▼
                                  IndexedDB ←→ liveQuery 推送
                                              │
                                              ▼
                              useObservable → Vue ref → UI 重绘
```

读路径：所有响应式订阅都是 `useLiveQuery(() => db.<table>.toArray())`，liveQuery 监听 IndexedDB 写事件 → 推 `useObservable` → Vue ref。**Dexie Cloud 的远程变更会先写本地表，再触发 liveQuery**，对业务代码透明。

写路径：业务代码同步写 IndexedDB → dexie-cloud-addon 后台异步推送至 Dexie Cloud；离线写入也能成功（本地优先）。

### 1.3 同步逻辑 ↔ 业务逻辑耦合点

无中间层。**32 个文件直接 `import { db }`** 并调用 `db.<table>.*` 或 `db.cloud.*`。具体高耦合区：
- `src/stores/workspaces.ts:12-65` — store 既存数据又写数据；删除工作区时直接 `db.transaction` 级联清 dialogs/messages/artifacts
- `src/views/DialogView.vue:486-1140` — 在 view 里 inline CRUD + 事务
- `src/composables/persistent-reactive.ts:5-29` — 把 `db.reactives` 当作通用 KV 用，`user-data` store 等都依赖它
- `src/pages/AccountPage.vue:216-272` — 直接调 `db.cloud.{login,logout,sync}` 并展示 license 状态

### 1.4 同步逻辑对外接口（其他模块的入口）

非显式 API，目前以四种形式暴露：
1. **Pinia store 的属性**（liveQuery 驱动的响应式数组）—— 业务读路径几乎全走这条
2. **`db.cloud.currentUser` Observable** —— `useObservable` 包装后给 UI 用（账户按钮、定价页、登录对话框）
3. **命令式 `db.cloud.{login, logout, sync, userInteraction}`** —— 在页面里直接调
4. **`db.on('ready', ...)` 事件钩子** —— `AccountPage.vue:218`、`ModelPricing.vue:176`

**没有统一的 `syncState` / pending / error 暴露**，UI 也没有同步状态指示条。

---

## Part 2 — 服务端优先迁移方案（分阶段，每阶段可独立验证）

### 总体策略

- **抽象一次，替换多次。** 仅 Stage 0 触达全部 32 个调用点，引入 `Repository` 接口；后续阶段只换实现。
- **特性开关并存。** 前端：`BACKEND_DATA_API_URL` + `BACKEND_DATA_TABLES`（CSV，逐表灰度）+ `BACKEND_AUTH`；后端：`BACKEND_DATA_API_ENABLED`（条件挂载 data API 路由）。`DexieDBURL` 空时仍纯本地，新旧并行。
- **ID 不变。** 沿用客户端生成的 UUID (`genId()`)，同一 id 在两端都是权威，回滚不丢数据。
- **IndexedDB 角色降级。** Stage 0–4 仍是首屏读源，Stage 5 起退化为缓存 + 离线 outbox。

### Stage 0 — 引入 Repository / Auth 抽象层（纯重构，行为不变）

**目标**：消除 `db.<table>.*` 与 `db.cloud.*` 的直接调用；为后续阶段建立替换点。

**新增**
- `src/data/repositories/{workspaces,dialogs,messages,assistants,artifacts,plugins,reactives,avatars,items,providers}.ts`
- `src/data/types.ts` — `Repository<T,K>` / `Query<T>` / `SubscriptionHandle`
- `src/data/auth.ts` — 包 `db.cloud.{currentUser, login, logout, sync, userInteraction}`，导出 `AuthSource`
- `src/data/transactions.ts` — `runTx(scopes, fn)` 包 `db.transaction`
- `src/data/observe.ts` — `observe(querier)`，初始实现转发给 `useLiveQuery`

**接口草稿**
```ts
interface Repository<T, K = string> {
  get(id: K): Promise<T | undefined>
  list(query?: Query<T>): Promise<T[]>
  add(value: T): Promise<K>
  put(value: T): Promise<K>
  update(id: K, changes: Partial<T>): Promise<number>
  delete(id: K): Promise<void>
  observeList(query?: Query<T>): ShallowRef<T[]>
  observeOne(id: K): ShallowRef<T | undefined>
}
interface AuthSource {
  user: ShallowRef<{ email?: string; accessToken?: string } | null>
  userInteraction: ShallowRef<unknown>
  login(): Promise<void>
  logout(): Promise<void>
  sync(): Promise<void>
}
```

> **实施备注**：Stage 0 实际落地的接口比草稿更完整 —— 见 `src/data/auth.ts` 实际导出 `AuthSource` 还包含 `enabled: boolean`、`onReady(fn)`、`waitForFirstSync()`、`currentToken()`。Stage 1.5 的 `BackendAuthSource` 必须按实际接口实现，不是按草稿的最小集。

**改造点**：所有 stores、composables、`DialogView.vue`、账户相关组件改用 repo / authSource。

**验证**：`pnpm build` 通过；手测核心闭环（建工作区 / 建 dialog / 发消息 / 刷新；登录登出 if `DexieDBURL`）。可补 `tests/repos.spec.ts` 跑 fake-indexeddb（不阻塞）。

**回滚**：纯机械重构，revert PR 即可。

---

### Stage 1 — 后端最小骨架 + 首张表（`providers`）

> **执行顺序**：Stage 1 内部分 7 个 Step。原 Step 3（JWKS 桥接鉴权）已废弃，被 **Stage 1.5（自家 JWT 多用户鉴权）** 整体替代。真实执行顺序：
> **Step 1 ✅ → Step 2 ✅ → Stage 1.5 → Step 4 → Step 5 → Step 6 → Step 7 → Stage 2**
>
> **上线策略**：Stage 1 全部 7 个 Step + Stage 1.5 完成后才算"Stage 1 收尾"，整体按「**上线但不启用**」策略合并到 my-deploy：代码合上线，但前端 `BACKEND_DATA_API_URL` / `BACKEND_DATA_TABLES` / `BACKEND_AUTH` 与后端 `BACKEND_DATA_API_ENABLED` 默认全不开 → 行为与 Stage 0 完全等同；按需在部署里开 flag 灰度。回滚不需 revert，关 flag 即可。

#### Step 1 — 后端骨架 + Postgres + 健康检查 ✅ commit `e4d3210`

**做什么**：`docker-compose.yml`（Postgres 16 容器映射到 host:5433）；`src-backend/data/db.py`（SQLAlchemy 异步 engine + session）；`app.py` 加 `/api/v1/health`；`alembic init`。

**通过判据**：`curl /api/v1/health` 返回 `{status:"ok", db:"ok"}`；`docker compose exec postgres psql ...` 能连。
- api: `tests/api/test_health.py::test_health_returns_ok`

#### Step 2 — `providers` 表 + 基础 CRUD（**先不带鉴权**）✅ commit `62318f6`

**做什么**：`models/provider.py`（含 `version` / `updated_at` / `deleted_at`）；首个 alembic migration（建表 + `global_change_seq` SEQUENCE）；`routers/providers.py` 提供 GET/PUT/DELETE /api/v1/providers[/:id] + `?since=`；`user_id` 暂以 `DEV_USER_ID` 环境变量占位。

**通过判据**（纯 curl，零前端依赖）：6 场景全过 — PUT 新增 / list / get / PUT 更新 bumps version / `?since=N` 过滤 / soft-delete 后 list 仍能拿到 tombstone；Postgres 直查行存在。
- api: `tests/api/test_providers.py::test_put_creates_and_list_returns_it`（PUT 新增 + list + Postgres 直查）
- api: `tests/api/test_providers.py::test_get_returns_single_row`（GET）
- api: `tests/api/test_providers.py::test_put_update_bumps_version`（PUT 更新 bumps version）
- api: `tests/api/test_providers.py::test_since_filter_drops_older_revisions`（`?since=N` 严格大于）
- api: `tests/api/test_providers.py::test_soft_delete_yields_tombstone_in_list`（DELETE → tombstone）
- api: `tests/api/test_providers.py::test_unauth_request_rejected`（鉴权后兜底 401）

#### ~~Step 3 — JWKS 桥接鉴权~~（已废弃 → Stage 1.5）

原计划在此处用 Dexie Cloud 的 JWKS 桥接鉴权，但实测 Dexie Cloud 不公开 JWKS（见 修订记录）。**改由 Stage 1.5 用自家 JWT 一次性解决多用户鉴权**，不再走 JWKS 桥接路线。

**Stage 1.5 完成后再继续 Step 4-7。**

#### Step 4 — 前端 HTTP 层

**做什么**：`src/data/http.ts` — fetch 封装，自动带 `Authorization: Bearer <authSource.currentToken()>`，统一 JSON 解析与错误处理；401 自动触发一次 refresh 后重试；不接任何 repository。

**通过判据（浏览器 Console 调）**：`http.get('/api/v1/providers')` 返回数组；Network 面板能看到 `Authorization` header。

#### Step 5 — `providers.server.ts`（REST 实现 Repository 接口）

**做什么**：`src/data/repositories/providers.server.ts` 实现 `Repository<CustomProvider>`；写操作先调 server，成功后回填 `db.providers`（缓存）；读优先本地缓存 + 后台 `?since=` 拉增量；暂不接 flag，提供手动开关函数供 Console 测试。

**通过判据（Console 调，绕开 UI）**：`put({id:'test1',...})` → server 与本地缓存都有 → `db.providers.clear()` → `list()` → 数据从 server 回灌。

#### Step 6 — Flag 路由（按环境变量动态选实现）

**做什么**：`src/data/repositories/index.ts` 根据 `BACKEND_DATA_API_URL` + `BACKEND_DATA_TABLES` 选 `providers.server` / `providers.dexie`；用 `SERVER_CAPABLE_TABLES` allowlist 防止 `BACKEND_DATA_TABLES` 拼错时静默跳过 Dexie；类比扩展给后续表预留位。

**通过判据**：A. flag 关 → 行为完全等同 Stage 0，Postgres 无新数据；B. flag 开 → 新数据落 Postgres，IndexedDB 也有缓存；C. flag 来回切 → 已在缓存的旧数据仍可读。

#### Step 7 — 端到端验证（Stage 1 收官）

**4 个场景**：
1. **清缓存恢复**：开 flag → 加 3 个 provider → 清 IndexedDB → 刷新 → 3 个回来
2. **跨设备同步**：浏览器 A、B 都登 backend 账号 X → A 改 → B 刷新看到（实时由 Stage 2 接管，本阶段允许刷新）
3. **flag 关闭回滚**：关 flag → 刷新 → UI 走缓存数据 → 行为回到 Stage 0
4. **双向兼容**：flag 关时建 Q1（走 Dexie）→ 切 flag 开 → Q1 不会自动出现在 Postgres（迁移机制属于「现有用户数据迁移」段，Stage 3+ 才生效）但 IndexedDB 仍能读

**Stage 1 出口判据**：4 个场景全过 + bundle 体积变化可接受 + flag 默认关时与 Stage 0 行为字节级一致 → 可合并到 my-deploy 上线。

---

### Stage 1.5 — 自家鉴权（多用户 Ready）

**目标**：上线一套自家管控的多用户鉴权，作为后续所有 `/api/v1/*` 端点的统一身份来源。Stage 5 不再需要做"切换鉴权"。

**后端新增**
- `src-backend/data/models/user.py` — `User(id, email UNIQUE, password_hash, status, linked_dexie_email NULL UNIQUE, created_at, last_login_at)`。`linked_dexie_email` first-write-wins，二次改写需管理员介入（见下文「用户身份关联」）。
- `src-backend/data/models/refresh_token.py` — `RefreshToken(id, user_id FK, token_hash, expires_at, revoked_at NULL, created_at)`
- `src-backend/data/auth.py` —
  - `bcrypt`/`passlib` 哈希密码；`PyJWT` 签 HS256（`JWT_SECRET` 来自 env，强制非空）
  - `current_user(token: Bearer)` 依赖项：解码 + 验签 + 校验 `exp` + 查 `users.status`，返回 `User` 模型
  - access token 30 min；refresh token 30 day（保存哈希到 `refresh_tokens` 表，可吊销）
- `src-backend/data/routers/auth.py` —
  - `POST /api/v1/auth/register`（email + 密码强度规则）
  - `POST /api/v1/auth/login` → `{access_token, refresh_token, user}`
  - `POST /api/v1/auth/refresh`
  - `POST /api/v1/auth/logout`（吊销当前 refresh token）
  - `GET /api/v1/auth/me`
  - `POST /api/v1/auth/link-dexie`（body: `{dexie_email}`；first-write-wins，已有 link 返回 409）
- `routers/providers.py` 把 `_current_user_id` 占位换成 `Depends(current_user)`，user_id 取 `user.id`
- 注册开关 env `ALLOW_REGISTRATION=true|false|invite`；invite 模式下需要 `INVITE_CODE`

**前端新增**
- `src/data/auth.ts` 新增 `BackendAuthSource` 实现 `AuthSource`：
  - `login()` 弹自家邮箱 + 密码表单（最小可用，复用 Quasar Dialog）
  - 内存 + `localStorage` 持久 access/refresh token；自动刷新
  - `currentToken()` 返回 backend access token，供 `src/data/http.ts` 使用
- 通过 flag `BACKEND_AUTH=true` 决定 `authSource` 默认实现挂哪一个：
  - `BACKEND_AUTH=true` → `BackendAuthSource`
  - 否则 → 现有 `DexieAuthSource`（Stage 0 实现，行为不变）
- `src/router/routes.ts`：`/account` 页在 `BACKEND_AUTH=true` 时绑定 `BackendAuthSource`，否则维持原逻辑

**与 Dexie Cloud 共存策略（双写窗口）**
- Stage 1.5–4 期间，前端**同时**保留 `dexie-cloud-addon` 挂载与 `BackendAuthSource`：
  - dexie 那边的登录用于继续读写「未迁移到 backend 的表」（Stage 1 灰度只有 `providers` 走 backend，其他表照旧 Dexie Cloud）
  - backend 那边的登录用于读写「已迁移的表」
  - 两套登录态独立，UI 上分两个入口（"Dexie 账号" / "本应用账号"），过渡期可接受
- Stage 5 摘掉 `dexie-cloud-addon` 后，只剩 `BackendAuthSource`，UI 退回单一登录入口

**用户身份关联（为后续数据迁移铺路）**
- 用户首次以 backend 账号登录、且浏览器同时还在 Dexie 会话里时，前端把当前 Dexie 的 `currentUser.email` 作为 `linked_dexie_email` 调一次 `POST /api/v1/auth/link-dexie`
- 后端写到 `users.linked_dexie_email`（NULL UNIQUE 列），给 Stage 3+ 的「现有用户数据迁移」环节当 mapping key
- **安全模型**：first-write-wins。一旦某 backend 账号 link 了某 dexie email，再次 link 不同 email 返回 409；同一 dexie email 也不能被两个 backend 账号同时 link（UNIQUE 约束保证）。改 link 需管理员手动 SQL 介入。后端无法实时验证客户端确实在 Dexie 会话中——这是已知弱点，靠 first-write-wins + UNIQUE 限缩攻击面。
- 若用户没在 Dexie 里登录、或主动跳过，迁移环节会让用户手动选择"把当前 Dexie 数据导入到当前 backend 账号"

**Migration**
- `users` 表 + `refresh_tokens` 表的 alembic migration

**验证（4 个独立场景）**
1. 注册新账号 A、B → `auth/me` 各返回各的 `user_id`
2. A 登录后 PUT `/api/v1/providers/p1` → 切到 B 登录后 GET 看不到 p1（账号隔离）
3. access token 故意过期 → 客户端自动 refresh → 不需要重新输密码
4. logout 后 refresh → 401（吊销生效）

- api: `tests/api/test_auth.py::test_register_returns_distinct_user_ids`（场景 1）
- api: `tests/api/test_auth.py::test_register_duplicate_email_returns_409`、`::test_password_min_length_enforced`（场景 1 边界）
- api: `tests/api/test_auth.py::test_login_returns_token_pair`、`::test_login_with_wrong_password_returns_401`、`::test_login_unknown_email_returns_401`（场景 1/3 前置）
- api: `tests/api/test_auth.py::test_a_token_cannot_read_b_data` + `tests/api/test_providers.py::test_account_isolation`（场景 2）
- api: `tests/api/test_auth.py::test_expired_access_token_yields_401`、`::test_refresh_issues_fresh_pair_and_revokes_old`（场景 3）
- api: `tests/api/test_auth.py::test_logout_revokes_refresh_token`、`::test_logout_unknown_refresh_is_idempotent`（场景 4）
- api: `tests/api/test_auth.py::test_me_without_token_returns_401`、`::test_me_with_garbage_token_returns_401`（unauth 兜底）
- api: `tests/api/test_auth.py::test_link_dexie_first_write_wins`（link-dexie first-write-wins，对应「用户身份关联」段安全模型）
- spec: `tests/e2e/stage1_5/auth-ui.spec.ts`（providers-rest profile 走 BackendLoginDialog UI 完整链路：注册 → currentToken 起效 + AccountPage 显示用户 → AccountPage 退登 → currentToken 清空；及刷新 boot 路径）

**回滚**：env `BACKEND_AUTH=false` 立即回到 Stage 0 行为；后端的 `users` 表和 endpoints 留着，不影响。

**风险**
- **JWT_SECRET 泄露** = 全部用户冒充。**缓解**：env 文件不入 git；生产用密钥管理服务；定期轮换（轮换时所有 access token 失效，用户需重新登录）。
- **无邮箱验证** = 注册阶段允许任意邮箱。**缓解**：MVP 阶段默认 `ALLOW_REGISTRATION=invite`，仅自己持有邀请码；公网开放时再补 SMTP + 验证邮件。

---

### Stage 2 — 实时订阅通道（`SyncSource` 抽象）

**目标**：让 server 在写发生后主动推给所有在线 Tab，Tab 收到后回填本地 IndexedDB → liveQuery 自动触发 → UI 自动刷新。业务代码（仍调 `repos.<table>.observeList()`）无感切换。Stage 1 的「Tab A 改 → Tab B 必须 F5」问题在这一阶段消除。

**核心机制（业界实践对齐）**
- **Pub/sub**：单进程内存 broker（`asyncio.Queue` per 订阅），多实例扩展时切 Redis pub/sub。参考 Centrifugo / NATS / Phoenix Channels MVP 起步同款。
- **事件来源**：API 写路径手动 publish（不挂 LISTEN/NOTIFY 也不挂 WAL 复制槽）。简单可控、不依赖 DB 特性。
- **多路复用**：单 WS / 多表订阅 / 客户端 refcount。参考 GraphQL Subscriptions、Supabase Realtime。
- **鉴权**：JWT 走 `Sec-WebSocket-Protocol` 子协议传递，避免 query string 进网关日志。
- **心跳**：Server 每 25s 主动 ping，10s 内无 pong 关连接（RFC 6455 Ping/Pong）。
- **断线补漏**：Outbox 表推迟到 Stage 3 引入；Stage 2 单表（providers）场景下，重连发 `since=<lastRev>` → server 直接 `SELECT * FROM providers WHERE user_id=? AND version > ?` 回放。
- **背压**：订阅队列 `maxsize=200`，满了直接 close 慢客户端，让它走重连补漏路径，避免 OOM。
- **降级**：WS → SSE → poll。**poll 直接复用 Stage 1 已有的 `GET /api/v1/providers?since=<rev>`，不新增端点**。
- **token 过期**：server 侧跟踪 JWT `exp`，到期主动 close（code 4001）→ 客户端 refresh → 重连。

> **执行顺序**：Stage 2 内部分 6 个 Step。
> **Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 → Stage 3**
>
> **上线策略**：与 Stage 1 同款「上线但不启用」。`REALTIME_TRANSPORT` 默认空 → 走 Stage 1 的「写后刷新看到」路径；按需在 Northflank 控制台开 flag 灰度。回滚不需 revert，关 flag 即可。

#### Step 1 — 后端：内存 pub/sub broker + WS 端点

**做什么**
- `src-backend/realtime.py`：
  - `Broker` 类 —— `subscribers: dict[user_id, set[Subscription]]`；`publish(user_id, event)` 推到所有订阅；`Subscription` 持有 `asyncio.Queue(maxsize=200)` + 表过滤集合
  - 进程级单例，按 `_enable_backend_data_api()` flag 守卫 lazy import（与 auth/providers 同款）
- `src-backend/data/routers/realtime.py`：
  - `WebSocket /api/v1/stream` 端点
  - 鉴权：客户端连接时把 `Sec-WebSocket-Protocol: bearer.<jwt>` 发上来；server 解析子协议拿 token，复用 `auth._decode_access`
  - 协议（JSON 文本帧）：
    - 客户端 → server：`{type:'subscribe', table, since?}` / `{type:'unsubscribe', table}` / `{type:'pong'}`
    - server → 客户端：`{type:'event', table, op:'put'|'delete', id, row?, rev}` / `{type:'ping'}` / `{type:'replay-done', table, rev}` / `{type:'error', code, message}`
  - 订阅时若带 `since`：先 `SELECT * FROM <table> WHERE user_id=? AND version > ? ORDER BY version` 回放，最后发一条 `replay-done` 让客户端知道追上了
  - 心跳：每 25s 发 ping；10s 内无 pong 主动 close
  - JWT exp 监听：`asyncio.create_task` 起一个定时器，到 exp 时 close(4001)
- `src-backend/data/routers/providers.py`：在 PUT / DELETE 的 `await session.commit()` 之后调一次 `broker.publish(user.id, {...})`
- `src-backend/app.py`：把 `realtime` router 加进 `_enable_backend_data_api()` 的挂载列表

**通过判据**（纯 Python 测试脚本，零前端依赖）
1. 两个 `websockets` 客户端用 user A 的 token 连上 `/api/v1/stream` 并 subscribe `providers` → curl PUT 一个 provider → 两个连接都收到事件
2. user A 的 WS 连接对 user B 的 PUT 不应收到事件（账号隔离）
3. 客户端发 `{type:'subscribe', table:'providers', since:0}` → 收到全部历史 + `replay-done` → 之后再 PUT → 收到 live 事件
4. 故意让客户端不发 pong → 35s 内被 close

- api: `tests/api/test_realtime_ws.py::test_ws_without_subprotocol_is_rejected`、`::test_ws_with_invalid_token_is_rejected`、`::test_ws_accepts_valid_token`（WS 鉴权）
- api: `tests/api/test_realtime_ws.py::test_ws_unknown_table_returns_error_frame`（未知表降级 error 帧而非 close）
- api: `tests/api/test_realtime_ws.py::test_ws_two_subscribers_same_user_both_receive`（场景 1 双订阅 fan-out）
- api: `tests/api/test_realtime_ws.py::test_ws_account_isolation`（场景 2 跨账号隔离）
- api: `tests/api/test_realtime_ws.py::test_ws_replay_emits_existing_rows_then_replay_done`、`::test_ws_replay_done_then_live_event`、`::test_ws_live_delete_event_is_tombstone`（场景 3 replay → live + delete tombstone）
- api: `tests/api/test_realtime_ws.py::test_ws_unsubscribe_stops_events`（unsubscribe）
- api: `tests/api/test_realtime_ws.py::test_ws_heartbeat_timeout_closes_connection`（场景 4 心跳超时，slow mark）

#### Step 2 — 前端：`SyncSource` 接口 + `DexieSyncSource`（纯重构）

**做什么**
- `src/data/sync-source.ts`：
  ```ts
  interface ChangeEvent<T> { op: 'put' | 'delete'; id: string; row?: T; rev?: number }
  interface SyncSource<T> {
    snapshot(): Promise<T[]>
    subscribe(onChange: (e: ChangeEvent<T>) => void): () => void
  }
  ```
- `DexieSyncSource<T>`：内部包 `liveQuery(() => table.toArray())`，把 diff 翻译成 `ChangeEvent`
- `src/data/repositories/dexie.ts` 的 `observeList` / `observeOne` 改为内部使用 `DexieSyncSource`（接口不变，业务无感）

**通过判据**
- `pnpm build` 通过（含 type check）
- 手测：建工作区 / 建 dialog / 发消息 / 跨标签页 Dexie Cloud 同步 —— 行为与 Stage 1 字节级一致

#### Step 3 — 前端：`RemoteSyncSource`（WS-only，不做降级）

**做什么**
- `src/data/realtime-ws.ts`：
  - `RealtimeConnection` 单例 —— 整个 app 一个 WS
  - 连接时构造子协议头 `bearer.<token>`（从 `authSource.currentToken()` 取）
  - 订阅 API：`subscribe(table, onEvent): unsubscribe`；内部按 table refcount，refcount=1 时发 server `subscribe`，refcount=0 时发 `unsubscribe`
  - 重连：指数退避 250ms → 8s（带 ±20% 抖动），上限 30s；重连成功后用每张表的 `lastRev` 重新 subscribe
  - 心跳：收到 server `ping` 立刻回 `pong`
  - 关闭码 4001（token expired）→ 调 `authSource` 触发 refresh → 重连
- `RemoteSyncSource<T>`：实现 `SyncSource` 接口，内部用 `RealtimeConnection.subscribe`
- 暂不接 repository（仍是 console 测试模式）

**通过判据**（Console 调，绕开 UI）
- 场景 A：Console 调 `realtime.subscribe('providers', e => console.log(e))` → 第二 tab PUT → 看到 event
- 场景 B：主动断网 5s 再连 → Console 看到自动重连日志，订阅恢复
- 场景 C：让 token 过期（手动 setTimeout 改本地 token）→ 看到 close 4001 → refresh → 重连

**当前状态（2026-05-02）**
- 代码：commit `aa0f25b`（`src/data/realtime-ws.ts` + `src/data/index.ts` 导出）；副 commit `e73e52e`（AccountPage 退出登录级联）
- 场景 A ✅ **通过**：Console 订阅后能收到 event（WS 握手、`bearer.<token>` 子协议鉴权、`since=<lastRev>` SQL replay、event 派发到 listener 全部 OK）。`'idle'` 边界 bug 已由「已知问题 #1」修复消除（见下方）。
- 场景 B ✅ **由 Phase 6 自动化覆盖**：`tests/e2e/stage2/step3-realtime-recovery.spec.ts::scenarioB ws auto-reconnect after offline window`，realtime-ws profile 走 `window.aiawRealtime.subscribe` + `setOffline` 闭环验证 backend 写入在重连后由 SQL replay 追上。
- 场景 C ✅ **由 Phase 6 自动化覆盖**：`tests/e2e/stage2/step3-realtime-recovery.spec.ts::scenarioC client recovers from server-side 4001 close`，模拟 server 4001 触发 client 走 `tryRefresh` → 重连 → 后续 backend put 仍能进 listener。
- spec: `tests/e2e/stage2/step3-realtime-recovery.spec.ts`

**已知问题**
1. ~~**`RealtimeConn` 进入 `'idle'` 后无自动恢复**~~ ✅ **已修复**
   - 原现象：`ensureConnected()` 检查 token，token 暂时为 null（如 reconnect 路径里 refresh 还在飞 / HMR 重置单例）→ 状态置 `'idle'` 直接 return，再无定时器 / 监听器把它推回连接尝试。只有新的 `subscribe()` 调用能触发 `ensureConnected()`。
   - 影响：场景 B 重连测试不可靠；场景 C token 过期路径理论上由 `tryRefresh().then(scheduleReconnect)` 兜底，但若 refresh 失败会一样卡死。
   - 修复：`realtime-ws.ts` 加 `wakeIfIdle()` + 模块级 `watch(authSource.user)`，null→user 跳变时主动叫醒。覆盖冷启动 subscribe 早于登录、4001 + refresh 失败后重登两条主路径。详见「修订记录 2026-05-02 · 已知问题 #1 修复」。

2. ~~**（Step 4 前置疑点）UI 不刷新疑似 dexie-cloud-addon middleware 吞读写**~~ ✅ **已修复**
   - 原现象：仅登 backend 账号（未登 Dexie）时，Console PUT provider → server 200 OK → WS event 派发到 listener 正常 → 但 UI 列表无新条目；`db.providers.toArray()` 直查 Promise 挂 pending。
   - 根因：dexie-cloud-addon 把所有非 `$` 表默认标 `markedForSync = true`，三个 dbcore middleware（`idGeneration` / `implicitPropSetter` / `mutationTracking`）对这些表的 readwrite 路径并入 `$<table>_mutations` 事务并写 `owner`/`realmId`，依赖 `trans.currentUser` 上下文，与 backend-only 用户冲突。
   - 修复：方案 (b) 的精细化版本——`src/utils/db.ts` 给 `db.cloud.configure(...)` 加 `unsyncedTables`（按 `BackendDataTables ∩ SERVER_CAPABLE_TABLES` 计算）。flag 开时 `providers` 退化为纯 Dexie 表，三个 middleware 全部短路；其它仍走 Dexie Cloud 的表不受影响。详见「修订记录 2026-05-02 · 已知问题 #2 修复」。

3. **（旁支跟踪，非本 Step 范围）`persistent-reactive` 脏状态竞态**
   - 现象：先登 backend、再登 Dexie 场景下，第一次输入正确验证码后 UI 无反应、刷新才生效；且加载的数据缺失默认 provider / 默认模型 / 系统助手等设置（`persistent-reactive` KV 存的字段），第二次重试又能恢复。
   - 历史：commit `4258fa8` 修过 clean-state 路径，dirty-state 在 5s 超时边界仍有问题。
   - 决策：跟踪中、不阻塞 Stage 2；Stage 3 迁 `reactives` 表时一起重新审视。

4. **（旁支跟踪）Dexie Cloud 登录 preflight 400**
   - 现象：本机环境某些时刻点"登录 Dexie 账号" → `dexie-cloud-addon.js:5780` 报 `TypeError: Load failed`，Network 看到 SaaS 端 `/login` preflight 400。
   - 怀疑：dev server 端口或本机环境的 CORS 触发 SaaS 侧策略，或 Dexie SaaS 临时问题。
   - 决策：需稳定重现条件才能定位；Stage 5 摘 addon 后自然消失。

#### Step 4 — 把 `providers.server.ts` 接到 `RemoteSyncSource`

**做什么**
- `providers.server.ts` 的 `observeList` / `observeOne` 改为：
  - 内部仍返回 `DexieSyncSource` 的 ref（这样 UI 读路径不变，liveQuery 仍驱动 Vue ref）
  - 同时启动一个 `RemoteSyncSource` 订阅，事件到来时把 row 写入 `db.providers` 缓存（put / delete）→ liveQuery 触发 → UI 自动更新
  - 这是「**cache-backed reads + server-driven cache writes**」混合模式：业务读永远走本地缓存，远端只负责让缓存保持最新
- 首次订阅时带 `since=<本地 db.providers 里最大 version>`，server 回放追上

**通过判据**
- Tab A、Tab B 都开 `BACKEND_DATA_API_URL` + `BACKEND_AUTH` + `BACKEND_DATA_TABLES=providers` + `REALTIME_TRANSPORT=ws`
- A 创建 / 修改 / 删除 provider → B 在 500ms 内 UI 更新（不刷新页面）
- B 离线 30s 期间 A 改 3 次 → B 网络恢复 → 重连 → 3 次变更追上 → UI 一致
- `REALTIME_TRANSPORT` 改空 / poll → 退化到 Stage 1 行为，A 改后 B 必须刷新

- spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case1 ws double-tab`（实时联动）
- spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case2 ws reconnect catch-up`（30s 离线追平）
- spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case3 providers-rest no-realtime`（无 WS 退化）
- spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case4 baseline byte-identical`（flag 全关零 9011 / 零 ws）

#### Step 5 — 降级路径（SSE + poll）

**做什么**
- `REALTIME_TRANSPORT` 取值：`ws`（默认）/ `sse` / `poll` / `auto`
- 后端：`GET /api/v1/stream/sse?since=<rev>&tables=providers`
  - 标准 `text/event-stream`；`id:` 字段用全局 rev；EventSource 自动用 `Last-Event-ID` 重连
  - 鉴权：EventSource 不支持自定义 header → 用 `event-source-polyfill` 走 `Authorization` header
  - 路由按 `_enable_backend_data_api()` flag 守卫 lazy import（与 `realtime.py` 同款），无 flag 部署不应在 import 期触达
- 前端：
  - `SseSyncSource` —— 包 EventSource(polyfill)
  - `PollSyncSource` —— `setInterval` 调 Stage 1 已有的 `GET /api/v1/providers?since=<rev>`，5s 一次
  - `auto` 模式：先试 WS，连不上 / 连上立刻被代理 close 时降到 SSE，SSE 也挂时降到 poll
  - 当前实际生效的 transport 通过 `window.aiawRealtime.transport`（取值 `'ws' | 'sse' | 'poll'`）暴露，复用 `src/boot/expose-debug.ts` 已有的 `EXPOSE_DB=true` 守卫与现有 `window.aiawRealtime.subscribe` 同一钩子点，供 e2e 验证 auto 真正降到了哪一档

**脚手架增量**（与 plan 维护规则同步在 Step 5 落地 PR 内一并加进 `playwright.config.ts` / `tests/env/` / `tests/scripts/`）
- 3 个新 profile + 端口 + env 文件：
  - `realtime-sse` → 9012 → `tests/env/.env.test.realtime-sse`（`REALTIME_TRANSPORT=sse`，其余 flag 同 `realtime-ws`）
  - `realtime-poll` → 9013 → `tests/env/.env.test.realtime-poll`（`REALTIME_TRANSPORT=poll`）
  - `realtime-auto` → 9014 → `tests/env/.env.test.realtime-auto`（`REALTIME_TRANSPORT=auto`）
- helper 增量：`tests/e2e/helpers/net.ts` 新增 `blockSSE(context)` 一行 `route('**/api/v1/stream/sse**', r => r.abort())`；其余 spec 复用现有 `blockWS` / `dumpTable` / `expectRowSync` / `backend.putProvider` / `openContextsForUsers`，不新增其他 helper
- `tests/scripts/backend-start.sh` 的 `CORS_ALLOW_ORIGINS` 扩到 9012 / 9013 / 9014（`localhost` + `127.0.0.1` 各一份），与 9007/9008/9009 同款写法
- 新建 e2e spec：`tests/e2e/stage2/step5-transport-degradation.spec.ts`
- 新建 pytest spec：`tests/api/test_realtime_sse.py`
- `tests/README.md` §helper ↔ plan 词汇表追加 `blockSSE` 一行；§已知预期红段在 spec-first 提交时短暂登记 case1-4 的红信号，Step 5 代码落地后清空

**通过判据**
- `REALTIME_TRANSPORT=sse` → 双 tab 联动正常（延迟略高于 WS，可接受）
  - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case1 sse double-tab`
  - api: `tests/api/test_realtime_sse.py::test_sse_replay_then_live`
  - api: `tests/api/test_realtime_sse.py::test_sse_account_isolation`
- `REALTIME_TRANSPORT=poll` → 双 tab 5s 内最终一致（`test.slow()`）
  - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case2 poll eventual within 5s`
- `REALTIME_TRANSPORT=auto` 在拦掉 WS upgrade 时自动降到 SSE，再拦掉 SSE 自动降到 poll，端到端联动仍最终一致；transport 实际档位通过 `window.aiawRealtime.transport` 校验
  - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case3 auto falls back to sse when ws blocked`
  - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case4 auto falls back to poll when sse blocked`（`test.slow()`）
- SSE 鉴权与 `Last-Event-ID` 续传兜底（pytest，零前端依赖）：
  - api: `tests/api/test_realtime_sse.py::test_sse_without_token_rejected`
  - api: `tests/api/test_realtime_sse.py::test_sse_with_invalid_token_rejected`
  - api: `tests/api/test_realtime_sse.py::test_sse_last_event_id_resumes_from_rev`

**与 Step 6 关系**：Step 5 落地后，「降级闭环」类判据归属本 Step 的 spec；Step 6 仅 reuse 复跑 + 软性上线把关，不再重新定义降级判据。

#### Step 6 — 端到端验证 + 上线（Stage 2 收官）

> Step 6 不引入新功能，只做 Stage 2 整体回归 + 非脚手架可覆盖的「软性」上线把关 + 翻 Stage 2 上线 flag。所有「协议 / 实时通道 / 降级」类判据已在 Step 1-5 的 spec / api 里沉淀，本 Step 仅 reuse 复跑、不重写。

**回归判据（reuse 既有 spec / api，不重写）**
1. **WS 双 tab 联动**
   - spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case1 ws double-tab`
2. **重连补漏 / token 过期 / server 4001**
   - spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case2 ws reconnect catch-up`
   - spec: `tests/e2e/stage2/step3-realtime-recovery.spec.ts::scenarioB ws auto-reconnect after offline window`
   - spec: `tests/e2e/stage2/step3-realtime-recovery.spec.ts::scenarioC client recovers from server-side 4001 close`
3. **账号隔离（broker 不跨 user_id）**
   - api: `tests/api/test_realtime_ws.py::test_ws_account_isolation`
   - api: `tests/api/test_realtime_sse.py::test_sse_account_isolation`
4. **降级闭环（SSE / poll / auto）**
   - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case1 sse double-tab`
   - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case2 poll eventual within 5s`
   - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case3 auto falls back to sse when ws blocked`
   - spec: `tests/e2e/stage2/step5-transport-degradation.spec.ts::case4 auto falls back to poll when sse blocked`
5. **`REALTIME_TRANSPORT` 空时退化与 flag 全关字节级一致**
   - spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case3 providers-rest no-realtime`
   - spec: `tests/e2e/stage2/step4-providers-realtime.spec.ts::case4 baseline byte-identical`

**上线把关判据（脚手架不覆盖，本 Step 必须手测 + 把结果数字写进进度快照）**
- **bundle 体积变化**：分别用 `pnpm test:build --profile=baseline` 与 `pnpm test:build --profile=realtime-ws`（均强制 cache miss，可改一字符 env 触发）→ diff 两份 `tests/.builds/<profile>/<key>/assets/*.js` 的总大小，差值在 ~+10 KB ± 5 KB 内（含 EventSource polyfill）即视为可接受；记入快照「bundle KB 数」字段
- **后端无内存泄漏**：`pnpm test:backend:start` 起 9011 → 跑本 Step 新增的 `tests/scripts/soak.sh`（循环 PUT + 多 WS 订阅 1h，幂等可中断）→ 期间每 5 min 抓一次 `ps -o rss= -p <pid>`，结束时与启动 5 min 后基线相比涨幅 < 30 MB；记入快照「RSS 起止 MB 数」字段
- **延迟 p95**：暂不进自动化（`helpers/sync.ts.expectRowSync` 当前只 poll 不出分布）；本 Step 用浏览器 DevTools 手测 100 次 PUT，目测 95% 在 500 ms 内即可；记入快照「p95 ms 数」字段。Stage 3+ 若需要硬性 SLA，再扩 helper 收集 percentile

**Stage 2 出口判据**
- 「回归判据」5 类全部 reuse 复跑全绿（含 Step 5 新增的 SSE / poll / auto case）：`pnpm test:api && pnpm test:e2e -g "stage2|stage1_5|smoke"` 一把全绿
- 「上线把关判据」3 条手测结果（bundle KB / RSS MB / p95 ms 三个具体数）写进 plan 进度快照
- 走「上线但不启用」策略：合并到 my-deploy 时 `REALTIME_TRANSPORT` 默认空 → 行为字节级等同 Stage 1 收尾；按需在 Northflank 控制台开 flag 灰度

**回滚**：`REALTIME_TRANSPORT=` 空（关闭实时通道，退回 Stage 1 行为）；如有更严重问题，从 `BACKEND_DATA_TABLES` 摘掉 providers 退回 Stage 0 行为。

**风险**
- **重连风暴**：100 个 tab 同时重连撞 server。**缓解**：客户端指数退避 + 抖动；server 端连接数硬限制（`MAX_WS_PER_USER=20`），超过直接 reject。**对应自动化**：暂无（hard limit 触发态在 e2e 模拟成本高），靠「上线把关判据」soak 阶段的多 WS 订阅压力 + Step 1 `test_ws_account_isolation` 反向证明 broker 不会跨 user_id 串
- **漏事件**：server 重启 / 长时断网期间事件丢失。**缓解**：每个 event 带 `rev`，客户端记 `lastRev`，重连发 `since=<lastRev>` 让 server SQL 回放。**对应自动化**：`step4 case2 ws reconnect catch-up` + `step3 scenarioB ws auto-reconnect after offline window`
- **慢客户端拖垮 server**：订阅队列堆积。**缓解**：`maxsize=200` + 满了 close 客户端，让它走重连补漏。**对应自动化**：暂无（200 条事件灌入实测成本高），靠「上线把关判据」1h soak 的 RSS 涨幅曲线反向兜

---

### Stage 3 — 迁移叶子表（`reactives` / `avatarImages` / `installedPluginsV2` / `assistants`）

**目标**：先搬无 join、无级联的表。`reactives` 特殊：是 `persistentReactive('#user-data', …)` 的底层，需要 KV 形 endpoint，让 `persistent-reactive.ts` 透明走 `repos.reactives.observeOne(key)` / `put({key,value})`。

**每张表节奏**：SQLModel + router + Alembic migration + 一个 PR + flag 翻一张。

**验证**：每张表独立验证 — UI 增改删 → 第二 tab 实时更新 → 服务端行匹配 → IndexedDB 缓存被回填。跑一次 `ExportDataDialog` / `ImportDataDialog`，确认 `db.tables` 枚举仍能导出（缓存仍然完整）。

**回滚**：单表 flag 翻回 Dexie，缓存还在就能继续读。

---

### Stage 4 — 迁移联表 / 级联表（`workspaces` / `dialogs` / `messages` / `items` / `artifacts`）

**目标**：搬级联删除集群（`stores/workspaces.ts` 里的 `db.transaction` 是最难的一处）。

**后端**：`DELETE /api/v1/workspaces/:id?cascade=true` 在单个 Postgres 事务里完成级联；为 dialog 删除提供同款。`GET /api/v1/messages?dialogId=…&since=…` 让 `DialogView.vue` 的滚动加载继续可行。

**前端**：`runTx()` 对这些表走新的 `repos.batch(operations)` → `/api/v1/batch`；尚未迁移的表仍走 `db.transaction`。

**验证**：删除一个含多 dialog/messages/artifacts 的工作区 → 服务端清空 → 清空 IndexedDB 后刷新仍正确。

**回滚**：flag 翻回；id 稳定，级联幂等。

---

### Stage 5 — 摘除 `dexie-cloud-addon`

**目标**：拆掉双写窗口最后一块。鉴权早在 Stage 1.5 已经全部走 `BackendAuthSource`，本阶段只负责清理 dexie-cloud 残留。

- `src/utils/db.ts` 从 `addons` 摘掉 `dexieCloud`；所有表退化为本地缓存
- `src/router/routes.ts` `/account` / `/model-pricing` 改按 `BACKEND_DATA_API_URL` 注册（如 Stage 1.5 已完成则只确认）
- `package.json` 移除 `dexie-cloud-addon`
- 后端补 `/api/v1/export` / `/api/v1/import`，让现有导出/导入 UI 继续工作（沿用 `dexie-export-import` 的格式约定）
- 清理 UI 上的"Dexie 账号 / 本应用账号"双入口，回归单一登录入口

**验证**：全新浏览器登录 → 服务端拉全数据；export → 清缓存 → import 往返；卸载 PWA → 重装 → 登录 → 数据回来。Tauri / Capacitor 构建产物里 grep 确认无 `dexie-cloud` 残留。

**回滚**：保留一个版本同时挂载 dexieCloud（用环境变量 `LEGACY_DEXIE_CLOUD=true` 重新挂上 addon），鉴权层不动。

---

### 现有用户数据迁移（**硬要求：不丢一条数据**）

**起点的三种用户**
1. 仅本地用户（`DexieDBURL` 空）：数据只在 IndexedDB
2. Dexie Cloud 用户：IndexedDB 与 Dexie Cloud 各持一份（最终一致）
3. **Stage 1.5 之后**注册的全新 backend-first 用户：本地无历史数据，迁移机制对其是 noop（首次 list 即跳过 push，直接走「server → 本地缓存」）

**迁移机制 — 客户端驱动一次性 push（推荐）**

> **接入时机**：迁移机制从 **Stage 3 起**接入（叶子表迁移开始）。Stage 1 的 `providers` 表**不接迁移机制**——数据量小（单用户通常 < 20 行），用户重新填一次即可，避免在迁移机制本身没成熟时把唯一已上线的表搞坏。

每张表在 **Stage 3+** 启用 server 实现时，`Repository.server` 在首次构造时执行一次性引导：

```
1. 读迁移标记（存在 reactives 表 / LocalStorage 的 `data.migration.<table>` key）
2. 若未迁移：
   a. 若有 Dexie Cloud 挂载：await db.cloud.sync() / 等 syncState === 'in-sync'，
      确保 IndexedDB 是 Dexie Cloud 最新副本
   b. 检查 server 端 GET /api/v1/migrate/status，若该用户已被其他设备迁移过 → 直接跳过 push
   c. 否则：分批 PUT /api/v1/<table>/bulk（每批 200-500 行），所有 ID 沿用客户端 UUID，幂等
   d. 全部成功后写本地 + server 端迁移标记
3. 已迁移：跳过，进入正常读写
```

**关键属性**
- ID 不变，PUT 天然幂等：失败重跑安全
- 多设备：第二台开机时 server 已有数据 → 跳过 push，直接走「server → 本地缓存」回灌
- 大表分批（`messages` / `artifacts` 可能上万行）+ 进度条 UI
- 失败重试：失败批次保留在本地 `outbox` 表，下次开机继续，不阻塞首屏
- **双写窗口**：Stage 1–4 期间客户端**继续保留 dexie-cloud-addon 挂载**；新后端就算迁移崩了，本地 + Dexie Cloud 上的原数据完全不动，flag 一关即回旧逻辑
- Dexie Cloud unmount 推迟到 Stage 5，给迁移留至少 1–2 个版本的双写窗口
- 冲突策略：行级 `updatedAt` LWW；首次 push 时 server 表空，无冲突

**验证迁移本身**
- 后端加 `GET /api/v1/migrate/status` 返回每张表 `(server_count, last_migrated_at, client_count_reported)`
- 客户端在 Settings 加「同步状态」面板：本地 vs server 行数对比 + 重新触发迁移按钮
- 灰度先选「读多写少 + 行数小」表（providers 优先），早期问题暴露成本低

---

### 跨版本导入/导出兼容（**硬要求：与旧版格式完全互通**）

旧版用 `dexie-export-import` 库：
- `ExportDataDialog.vue:65` — `exportDB(db, options)` 产出 `aiaw_user_db.json`
- `ImportDataDialog.vue:84` — `importInto(db, file, opts)` 反向

**该格式是 Dexie 官方定义**（schema 元数据 + 各表行数组），不是 AIaW 私有格式。新版本必须保持同一格式可双向读写。

**实现策略**

新版的导出按钮：
1. **先把 server 端权威数据全量回灌本地缓存**（多设备 / 清过缓存的客户端 server 比本地多）
   - 调 `repos.<table>.list()` for all tables（带 `since=0` 走全量）
   - 写到 `db.<table>`（缓存）
2. 调 `exportDB(db, options)` —— 与旧版**字节级一致**
3. 文件名仍为 `aiaw_user_db.json`

新版的导入按钮：
1. `importInto(db, file, opts)` —— 与旧版完全一致，先把数据写入 IndexedDB（缓存）
2. 导入完成后：在 `Repository` 层触发一次"缓存 → server"双向 push（同上面的迁移机制，复用 `bulk PUT` endpoint）
3. 这样：旧版导出文件 → 新版导入 → 自动同步上 server；新版导出文件 → 旧版导入 → 旧版正常读写（旧版不知道 server 存在，但本地数据完整）

**前提**：新版的 `db.ts` schema 必须**保持兼容旧版**（同表名、同主键、同索引）。Stage 5 删除 `dexie-cloud-addon` 但 IndexedDB schema 不变，`exportDB`/`importInto` 仍能互通。

**验证**
- E2E：旧版导出 → 新版导入 → 数据完全一致（包括 `owner` / `realmId` 字段，新版应忽略而非报错）
- E2E：新版导出 → 旧版导入 → 数据完全一致（旧版只看 IndexedDB，看不到 server 但本地是全的）
- 单元：保留 `dexie-export-import` 依赖；保留 `ExportDataDialog.vue` / `ImportDataDialog.vue` 的现有 UI 与 API；只在 export 前加一步 server 拉取、import 后加一步 server push

---

### 横切关注点

- **Schema 真源迁移**：从 Stage 1 起以后端 Alembic 迁移为权威；客户端 `db.ts` 的 schema 在 Stage 5 后**仍保持与旧版兼容**（同表名 / 同主键 / 同索引），让 `dexie-export-import` 跨版本继续可用。
- **现存 reading hooks**：`db.ts` 里的几个 `db.<table>.hook('reading', ...)` v1.4/v1.8 兼容迁移要在对应表的迁移阶段移植到后端读序列化器，老客户端无需自己规整。
- **离线写**：Stage 5 前离线写仍由 dexie-cloud-addon 兜底；Stage 5 起新增 `outbox` 表，`RemoteSyncSource` 在重连时 flush。
- **观测**：`Repository` 接口层加 `data.repo.<table>.<op>` 计数器，灰度期可对比新旧实现错误率。
- **i18n / UI 状态条**：Stage 2 起补一个全局 `syncState` 暴露（`'idle' | 'syncing' | 'offline' | 'error'`），写进 `MainLayout` 顶栏，提前在迁移期就给用户可视化反馈。
- **后端模块条件挂载**：`src-backend/data/auth.py` 等模块在 import 期间会读 `JWT_SECRET` 并 fail-fast；`src-backend/app.py` 通过 `BACKEND_DATA_API_ENABLED` flag **延迟 import** data 路由（lazy import 在挂载函数内），避免单环境 misconfig 把 CORS 代理 / 文档解析 / SPA 静态等无关功能一起带崩。新增 backend 子模块（如 Stage 2 的 `realtime.py`）时同样应在 flag 守卫内 import，并把所需 env 加入挂载函数的 fail-fast 校验列表。

---

## 关键文件清单（供后续 PR 直接定位）

- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/utils/db.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/utils/config.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/composables/live-query.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/composables/persistent-reactive.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/composables/login-dialogs.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/stores/{workspaces,assistants,providers,plugins,user-data}.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/views/DialogView.vue`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/pages/AccountPage.vue`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/router/routes.ts`
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/app.py`（Stage 1 起 mount `src-backend/data/` 路由）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/realtime.py`（Stage 2 新增：WS pub/sub）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/models/user.py`（Stage 1.5 新增）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/models/refresh_token.py`（Stage 1.5 新增）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/auth.py`（Stage 1.5 新增：JWT 签验 + `current_user` 依赖）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/routers/auth.py`（Stage 1.5 新增：register/login/refresh/logout/me/link-dexie）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/data/auth.ts`（Stage 0 新增 `AuthSource`，Stage 1.5 加 `BackendAuthSource`）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/data/http.ts`（Stage 1 新增：从 `AuthSource` 取 token）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/package.json`（最终阶段移除 `dexie-cloud-addon`）

---

## 端到端验证策略

| 阶段 | 验证手段 | 通过判据 |
|---|---|---|
| 0 | `pnpm build` + 手测核心闭环 | 行为与现状完全一致 |
| 1 | staging + 单表（providers）灰度 | 清缓存后服务端数据回灌；flag 关掉立即回到旧逻辑 |
| 1.5 | 注册 A/B 两账号 + token 过期 + logout 吊销 | 账号数据隔离；token 自动 refresh；logout 后 refresh 返回 401 |
| 2 | 双 tab 实时联动 | < 500ms 收到事件；断网降级到 poll 仍最终一致 |
| 3 | 逐叶子表 PR + 导出/导入往返 | 每张表独立可灰度可回滚 |
| 4 | 级联删除 + 大量消息加载 | 服务端单事务级联，DialogView 滚动加载性能不退化 |
| 5 | 全新设备首次登录 + 卸载重装 | 服务端为唯一真源；包内无 `dexie-cloud-addon` |
| 全程 | **旧版导出 → 新版导入 → 旧版导入** 数据闭环 | `aiaw_user_db.json` 字节级互通，无字段丢失 |
| 全程 | 多设备开机迁移 | 第二台不重复 push；server 与本地行数一致 |

每阶段均能合并到 master、独立部署、按 flag 灰度，验证失败时仅通过环境变量回退即可，无需代码 revert。
