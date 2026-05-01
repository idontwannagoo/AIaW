# 云同步架构分析 + 服务端优先迁移方案

## Context

当前仓库 (AIaW fork) 的云同步基于 **Dexie + dexie-cloud-addon**（本地优先 / IndexedDB → 官方托管 SaaS `https://znm3rqzc8.dexie.cloud`）。后端 `src-backend/app.py` **不参与同步**，只做 CORS 代理与文档解析。业务代码 32 处直接 `import { db }` 调 `db.<table>.*`，没有数据访问层抽象，登录/账户/订阅 UI 直接耦合 `db.cloud.*` API。

目标是迁移到 **服务端优先**：自己的 FastAPI 承担权威存储，客户端通过 REST + WebSocket 读写，IndexedDB 退化为缓存。本计划重点在「**每一阶段都能独立合并、构建、上线、端到端验证**」，不需要全部改完才能跑通。

部署形态目标：**多用户 SaaS-style**（不仅仅是单人自部署），所以鉴权必须从一开始就支持多账号、可对外开放注册或受控开放。

---

## 修订记录

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
- **2026-05-02 · Stage 2 Step 1-3 代码落地 + plan 入仓库**
  - **变更**：Stage 2 前 3 个 Step 的代码已全部合并到 `feature/change-cloud-sync-claude` 分支（尚未合 my-deploy）；plan 文件本身从 `~/.claude/plans/` 复制到仓库 `plans/cloud-sync-migration.md`，CLAUDE.md 加「长期迁移工作流」段并入库（详见进度快照 2026-05-02）。
  - **影响范围**：plan 维护规则正式生效——动态进度（当前 Step、commit hash、未解决问题）一律写本文件，不写 CLAUDE.md；任何改动需配套通过判据，每个 Step 完成后必须更新「进度快照」段；方案有调整必须先在「修订记录」追加条目再改正文。
  - **后续维护**：本文件即权威版；`~/.claude/plans/1-ethereal-lollipop.md` 仅给 Claude Code `/plan` 命令读，按需手动 `cp` 仓库版同步。
- **进度快照（2026-05-02）**
  - **Stage 2 / Step 1**（后端 broker + WS `/api/v1/stream`）✅ commit `e6c5335`
  - **Stage 2 / Step 2**（前端 `SyncSource` 接口 + `DexieSyncSource` 重构）✅ commit `7755de7`
  - **Stage 2 / Step 3**（前端 `realtime-ws.ts` — `RealtimeConn` 单例 + `createRemoteSyncSource`）⚠️ **代码已合 commit `aa0f25b`，验收进行中**
    - 副 commit `e73e52e`（修复 AccountPage 主退出登录级联登出 Dexie 会话）— 联动测试时浮现的 Stage 1.5 时期遗留 bug，本次 Step 3 验收前一并修掉
    - 验收三个场景里**场景 A 部分通过**：Console `realtime.subscribe('providers', cb)` 能收到 event（WS 握手 / 子协议鉴权 / SQL replay / 派发都正常）
    - 验收**场景 B（主动断网重连）/ 场景 C（token 过期 close 4001 + refresh + 重连）尚未独立测试**
    - 详见下方 §Stage 2 / Step 3「当前状态」段
  - **plan 文件 + CLAUDE.md 入库** ✅ commit `f2a39d4`
  - 下一步：先把 Stage 2 / Step 3 验收收尾（场景 B/C + 修「`idle` 状态无自动恢复」边界 bug），再进 Step 4。

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

#### Step 2 — `providers` 表 + 基础 CRUD（**先不带鉴权**）✅ commit `62318f6`

**做什么**：`models/provider.py`（含 `version` / `updated_at` / `deleted_at`）；首个 alembic migration（建表 + `global_change_seq` SEQUENCE）；`routers/providers.py` 提供 GET/PUT/DELETE /api/v1/providers[/:id] + `?since=`；`user_id` 暂以 `DEV_USER_ID` 环境变量占位。

**通过判据**（纯 curl，零前端依赖）：6 场景全过 — PUT 新增 / list / get / PUT 更新 bumps version / `?since=N` 过滤 / soft-delete 后 list 仍能拿到 tombstone；Postgres 直查行存在。

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
- 场景 A ⚠️ **部分通过**：Console 订阅后能收到 event（WS 握手、`bearer.<token>` 子协议鉴权、`since=<lastRev>` SQL replay、event 派发到 listener 全部 OK）。但联动测试中观察到 `aiawRealtime.state` 偶发卡 `'idle'` —— 见下方「已知问题 #1」。
- 场景 B ❓ **未独立测试**：被场景 A 的 idle 边界 bug 与「UI 不刷新」诊断岔开。
- 场景 C ❓ **未独立测试**：同上。

**已知问题（待 Step 3 收尾时处理）**
1. **`RealtimeConn` 进入 `'idle'` 后无自动恢复**
   - 现象：`ensureConnected()` 检查 token，token 暂时为 null（如 reconnect 路径里 refresh 还在飞 / HMR 重置单例）→ 状态置 `'idle'` 直接 return，再无定时器 / 监听器把它推回连接尝试。只有新的 `subscribe()` 调用能触发 `ensureConnected()`。
   - 影响：场景 B 重连测试不可靠；场景 C token 过期路径理论上由 `tryRefresh().then(scheduleReconnect)` 兜底，但若 refresh 失败会一样卡死。
   - 决策方向：Step 3 收尾时给 `'idle'` 加一个轻量监听—— 监听 `authSource.user` 变化，token 重新可用时主动调一次 `ensureConnected()`。改动局限在 `realtime-ws.ts` 一个文件。

2. **（Step 4 前置疑点）UI 不刷新疑似 dexie-cloud-addon middleware 吞读写**
   - 现象：仅登 backend 账号（未登 Dexie）时，Console PUT provider → server 200 OK → WS event 派发到 listener 正常 → 但 UI 列表无新条目；进一步用 `db.providers.toArray()` 直查，Promise 挂 pending、`.catch` 也不触发。
   - 怀疑：`dexie-cloud-addon` 在没登录 Dexie 时对读写仍插中间件、卡在等"sync ready"或类似条件。
   - 范围：严格说不属于 Step 3（Step 3 判据是 Console 收 event，已通过）；属于 **Step 4**（UI 刷新）的前置阻塞。
   - 处置：留到 Step 4 接 `RemoteSyncSource` 时一起验证。届时若 `db.providers.put(row)` 仍被吞，候选缓解：
     - (a) 直接绕开 cloud middleware 写一张非 cloud 的镜像表
     - (b) 让 backend-only 用户的 `db.ts` 不挂 `dexieCloud` addon
     - (c) 等 Stage 5 摘 dexie-cloud-addon 后自然消失（但那要等很久）
   - 优先级：Step 4 启动前必须有结论，否则 Step 4 整套 cache-backed reads 模型走不通。

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

#### Step 5 — 降级路径（SSE + poll）

**做什么**
- `REALTIME_TRANSPORT` 取值：`ws`（默认）/ `sse` / `poll` / `auto`
- 后端：`GET /api/v1/stream/sse?since=<rev>&tables=providers`
  - 标准 `text/event-stream`；`id:` 字段用全局 rev；EventSource 自动用 `Last-Event-ID` 重连
  - 鉴权：EventSource 不支持自定义 header → 用 `event-source-polyfill` 走 `Authorization` header
- 前端：
  - `SseSyncSource` —— 包 EventSource(polyfill)
  - `PollSyncSource` —— `setInterval` 调 Stage 1 已有的 `GET /api/v1/providers?since=<rev>`，5s 一次
  - `auto` 模式：先试 WS，连不上 / 连上立刻被代理 close 时降到 SSE，SSE 也挂时降到 poll

**通过判据**
- `REALTIME_TRANSPORT=sse` → 双 tab 联动正常（延迟略高于 WS，可接受）
- `REALTIME_TRANSPORT=poll` → 双 tab 5s 内最终一致
- 在 nginx 前面挡掉 WS 升级 + `REALTIME_TRANSPORT=auto` → 自动降到 SSE，仍正常工作

#### Step 6 — 端到端验证 + 上线（Stage 2 收官）

**6 个场景**
1. **WS 双 tab 联动**：A 改 → B 在 500ms 内更新（前 95 百分位）
2. **重连补漏**：kill WS 连接 → A 改 3 次 → B 自动重连 → 3 次变更全到
3. **账号隔离**：A 账号 PUT，B 账号 WS 不应收到（直接看 server 日志确认 publish 不跨 user_id）
4. **token 过期**：access token TTL 调成 60s，挂着 WS 等 70s → 看到自动 refresh + 重连
5. **降级闭环**：`REALTIME_TRANSPORT=auto` + 手动挡 WS → 走 SSE；再挡 SSE → 走 poll；最终一致
6. **flag 默认关字节级一致**：`REALTIME_TRANSPORT` 不设 → 行为与 Stage 1 收尾时完全一致

**Stage 2 出口判据**：6 场景全过 + bundle 体积变化可接受（WS / SSE 客户端约 +10KB）+ 后端无内存泄漏（连续运行 1h 看 RSS 平稳）→ 合并到 my-deploy 上线。

**回滚**：`REALTIME_TRANSPORT=` 空（关闭实时通道，退回 Stage 1 行为）；如有更严重问题，从 `BACKEND_DATA_TABLES` 摘掉 providers 退回 Stage 0 行为。

**风险**
- **重连风暴**：100 个 tab 同时重连撞 server。**缓解**：客户端指数退避 + 抖动；server 端连接数硬限制（`MAX_WS_PER_USER=20`），超过直接 reject
- **漏事件**：server 重启 / 长时断网期间事件丢失。**缓解**：每个 event 带 `rev`，客户端记 `lastRev`，重连发 `since=<lastRev>` 让 server SQL 回放
- **慢客户端拖垮 server**：订阅队列堆积。**缓解**：`maxsize=200` + 满了 close 客户端，让它走重连补漏

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
