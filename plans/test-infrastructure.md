# 自动化测试脚手架方案

## Context

仓库 `pnpm test` 是 placeholder（`echo "No test specified"`），无任何测试套件。云同步迁移（见 `plans/cloud-sync-migration.md`）每个 Step 的「通过判据」目前几乎全靠 curl 临时脚本 + 浏览器多 tab 手测，验收成本高、回归不可累积、Claude Code 自动化困难。

本计划目标：搭一套**两层自动化测试体系**（pytest 后端 + Playwright 端到端），让所有 plan 级别的判据都能由 `pnpm test:*` 一条命令跑出红绿，**Claude Code 不需要用户在浏览器手动点击就能完成 Stage 2-5 全部验收**，且每个新 Stage / Step 的判据沉淀进回归套件。

测试体系本身是基础设施而非业务功能，因此独立成 plan，与 cloud-sync-migration plan 平行推进；每个 Phase 完成后允许阻塞下一个 cloud-sync Stage 的开工（确保未来 Step 一落地就有自动验收能力）。

---

## 修订记录

- **2026-05-02 · plan 初稿入库**
  - **背景**：Stage 2 Step 4 即将动工，若仍走手测验收，「双 tab + 切网络」类判据将无法被 Claude Code 自动跑。利用 Step 4 开工前先把脚手架做掉，作为 Step 4 验收的实际工具。
  - **决策 1**：两层测试架构（pytest 后端 + Playwright 端到端）。舍弃 Vitest 单元层——本项目 bug 几乎全是「多端协作 / 时序 / 网络 / 缓存层与 server 层不一致」型，纯单元覆盖不到，性价比低。等 Stage 5 摘 dexie-cloud 后再补几个核心 repository 的 Vitest。
  - **决策 2**：e2e 走 `quasar build` 产物 + 静态服务器，不用 dev server。原因：(a) `process.env.*` 是构建期内联，每个 flag profile 必须独立 build；(b) 构建产物启动比 dev 快、更接近线上；(c) profile build 用 sha256 cache 命中后 < 2s 复用，rebuild 摊销可接受。
  - **影响范围**：新增 `tests/` 顶层目录、`docker-compose.test.yml`、`playwright.config.ts`、`pytest.ini`；package.json 新增若干 `test:*` script；不动现有源代码（除 dev/test build 给 `window.__db__` / `window.__authSource__` 加 `EXPOSE_DB=true` 守卫的暴露开关）。

- **进度快照（2026-05-02）**
  - plan 文件入库 ✅ `aa15721`
  - Phase 1 测试环境底座 ✅ `8e61258`
    - docker-compose.test.yml（Postgres 16 → 5434，project name `aiaw-test`，volume `aiaw_test_pg`，pg_isready healthcheck）
    - tests/scripts/{test-up,test-down,backend-start,backend-stop}.sh（全 chmod +x）
    - package.json 加 `test:up` / `test:down` / `test:backend:start` / `test:backend:stop`
    - .gitignore 加 `/tests/.builds/` `/tests/.results/`
    - 判据真跑：`/api/v1/health` → `{status:"ok",db:"ok"}`；dev 5433/aiaw-postgres 不受影响；`test:down` 释放 5434 + 删 volume；二次 up/start 幂等；5434/9011 被占时 lsof 友好报错
  - Phase 2 前端 profile 构建管理 ✅ 本次提交
    - tests/env/.env.test.{baseline,providers-rest,realtime-ws}（含 EXPOSE_DB=true 给 e2e 钩子用）
    - tests/scripts/build-frontend-profile.mjs（cache key = sha256(profile + env-sha + git rev + pkg-sha)，trap-style .env.local 还原：finally + SIGINT/SIGTERM/SIGHUP/uncaughtException 多入口 idempotent restore）
    - tests/scripts/serve-build.mjs（plain node:http，SPA fallback：无扩展名路径 → index.html，带扩展名缺失 → 404；EADDRINUSE 友好报错）
    - package.json 加 `test:build` / `test:serve`
    - 判据真跑：cold baseline build 15.7s；二次跑 cache hit 0.38s；改 env 一字符 → cache miss + new key + 重 build 13.9s；env 复位 → 命中原 key（cache 确定性）；mid-build SIGINT 后 .env.local sha 与启动前一致；serve-build GET / → index.html、GET /workspaces/abc → SPA fallback 命中 index.html、GET /missing.png → 404
    - 备注：plan 原写 `.ts`，实际落地为 `.mjs`（plain Node ESM，无须 tsx/ts-node 等额外 dev dep；逻辑无 TS-only 特性）
  - Phase 3-7 未启动
  - 下一步：Phase 3（后端 pytest 层 + conftest fixture + Stage 1 / 1.5 / 2-Step1 判据沉淀）

---

## 设计原则

1. **判据即代码** —— cloud-sync-migration plan 每条「通过判据」必须能映射到一个 `pytest` test name 或 Playwright spec name；以后 plan 写判据时同时写下对应 spec 路径。手测判据规模性归零。
2. **Helper 投资 > Spec 数量** —— 长期价值在贴合 plan 词汇的 helper 集（`expectRowSync` / `setOffline` / `loginAsBackend` / `pgQuery`）。spec 本身应只有十几行业务调用，无重复脚手架。
3. **环境完全隔离** —— 测试用 Postgres 5434 / backend 9011 / 前端 9007，与 dev 的 5433/9010/9005 互不影响；docker-compose 一键起停，绝不触碰用户当前 dev 状态。
4. **构建产物驱动 e2e** —— 所有 e2e 跑在 `quasar build` 静态产物上，不用 dev server。每 flag 组合 = 一个 profile = 一份 build，按 sha256 cache 复用。
5. **Claude-Code-operable** —— 单条命令一把跑完 + 退出码正确 + JSON reporter / trace 文件路径稳定可读；不依赖用户 GUI 操作。

---

## 架构总览

```
tests/
  api/                          # pytest — 后端
    conftest.py                 # 干净 DB / 两用户 / token / pg fixture
    test_health.py
    test_auth.py
    test_providers.py
    test_realtime_ws.py
    test_migrate.py             # Stage 3+ bulk push（Phase 7 补）
    test_export_import.py       # Stage 5 跨版本兼容（Phase 7 补）
  e2e/                          # Playwright — 端到端
    helpers/
      env.ts                    # profile → build 映射
      auth.ts                   # 登录助手（UI / 直接 API 两种路径）
      tabs.ts                   # 多 tab / 多用户 context 工厂
      db.ts                     # IndexedDB / Dexie 读写助手（page.evaluate）
      backend.ts                # 直接打后端 REST 种数据
      pg.ts                     # 直查 Postgres 校验
      ws.ts                     # 捕获 page.on('websocket') 帧
      net.ts                    # setOffline / blockWS / blockHost
      sync.ts                   # expectRowSync / waitForVersion
      io.ts                     # exportData / importData
      migration.ts              # Stage 3+ 用：seedDexieData / triggerMigration（Phase 7 补）
      cascade.ts                # Stage 4 用（Phase 7 补）
    smoke.spec.ts
    stage1/
    stage1_5/
    stage2/
      step4-providers-realtime.spec.ts
    stage3/
    stage4/
    stage5/
    legacy/                     # Stage 0 baseline 回归
  scripts/
    test-up.sh                  # docker compose up -d + 健康等待
    test-down.sh
    backend-start.sh            # 起 9011 backend (uvicorn)
    backend-stop.sh
    build-frontend-profile.ts   # profile build + sha256 cache
    serve-build.ts              # 静态托管 build 产物到 9007
  env/
    .env.test.baseline          # 全 flag 关
    .env.test.providers-rest    # backend providers / 无 realtime
    .env.test.realtime-ws       # + ws
    .env.test.full-cascade      # Stage 4 多表打开（Phase 7 补）
  fixtures/
    seed-users.json
  .builds/                      # gitignore — profile build 缓存
  .results/                     # gitignore — junit / json reporter 输出
  README.md

docker-compose.test.yml         # Postgres 5434
playwright.config.ts            # projects = profiles
pytest.ini
```

---

## Phase 拆分

### Phase 1 — 测试环境底座 ✅

**做什么**
- `docker-compose.test.yml`：Postgres 16 → 5434，独立 named volume `aiaw_test_pg`，含 `pg_isready` healthcheck。与 dev 5433 完全互不干扰。
- `tests/scripts/test-up.sh`：`docker compose -f docker-compose.test.yml up -d` + 等 healthcheck 通过。
- `tests/scripts/test-down.sh`：`docker compose down -v`，彻底清 volume。
- `tests/scripts/backend-start.sh`：用 `tests/env/.env.test.baseline` 起 uvicorn 在 9011，强制 `BACKEND_DATA_API_ENABLED=true` 与 `JWT_SECRET=<test-only>`，自动跑 alembic upgrade head。
- `tests/scripts/backend-stop.sh`：杀对应 PID。
- `package.json` 新增 `test:up` / `test:down` / `test:api` / `test:e2e` / `test:e2e:profile=<name>` 顶级 script，全部依赖 9011/5434，不抢占 dev 端口。
- `.gitignore` 加 `tests/.builds/` 与 `tests/.results/`。

**通过判据**
1. `pnpm test:up` 后 `curl http://localhost:9011/api/v1/health` → `{status:"ok",db:"ok"}`。
2. `pnpm test:up` 期间用户原本的 dev backend 9010（若在跑）不受影响；端口 5433 也不被触碰。
3. `pnpm test:down` 后端口 5434 / 9011 释放，`docker volume ls` 看不到 `aiaw_test_pg`。
4. 二次 `test:up` 时 alembic 不重复建表（idempotent）。
5. 启动前若 5434 / 9011 / 9007 已被别的进程占用，脚本应用 `lsof` 友好报错，不静默挂起。

---

### Phase 2 — 前端 profile 构建管理 ✅

**做什么**
- `tests/env/.env.test.<profile>`：先建 3 个（`baseline` / `providers-rest` / `realtime-ws`），后续 Stage 加表只新增 profile。
- `tests/scripts/build-frontend-profile.ts`：
  1. 读 `--profile=<name>` 参数。
  2. 计算 `(env-file-sha256, git rev-parse HEAD, package.json hash)` 联合 cache key。
  3. 命中 `tests/.builds/<profile>/<key>/` → 直接打印该路径。
  4. 未命中：把 `.env.test.<profile>` 临时拷成 `.env.local`（**带 trap 还原原始 .env.local，无论成功/失败/中断**），跑 `quasar build -m spa`，产物搬到 cache 目录，恢复 `.env.local`。
- `tests/scripts/serve-build.ts`：用 `sirv-cli` 把指定路径托管到 9007。Playwright `webServer` 配置直接调它。

**通过判据**
1. 干净状态首次 `build-frontend-profile.ts --profile=baseline` 产出 cache 路径，时长接近本机 `pnpm build`。
2. 立刻第二次跑 → 命中 cache，< 2s 返回路径。
3. 改 `.env.test.baseline` 内一字符 → 第三次跑 cache miss → 重 build。
4. 任何情况下脚本结束后 `.env.local` 文件内容与脚本启动前一致（用 git diff 验证）；中途 Ctrl+C 也不留垃圾态。

---

### Phase 3 — 后端 pytest 层

**做什么**
- 依赖：`pytest`, `pytest-asyncio`, `httpx`, `websockets`, `psycopg[binary]`，加进 `src-backend/requirements.txt` 的 dev extras（或单独 `requirements-dev.txt`）。
- `tests/api/conftest.py` 提供 fixture：
  - `db_reset`（function-scoped，默认）：`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` + `alembic upgrade head`。慢但隔离最干净。
  - `db_tx_rollback`（function-scoped，opt-in）：开事务、yield、ROLLBACK。给纯读 / 单连接 case 用。
  - `user_a` / `user_b`：直接调 `/auth/register` + `/auth/login`，返回 `{id, email, access_token, refresh_token}`。
  - `client_a` / `client_b`：预带 `Authorization` header 的 `httpx.AsyncClient`。
  - `ws_factory`：`async def connect(token: str) -> WebSocketClientProtocol`，封 `bearer.<token>` 子协议。
  - `pg_conn`：`psycopg.AsyncConnection` 直接打 5434。
- 把以下手测过但未沉淀的判据落成 case：
  - **Stage 1 Step 1**：`/health` 200 + db ok
  - **Stage 1 Step 2**：providers 6 场景（PUT 新增 / list / get / PUT 更新 bumps version / `?since=N` / soft-delete tombstone）
  - **Stage 1.5**：register / login / refresh / logout 吊销 / 账号隔离（A 的 PUT B 看不见）
  - **Stage 2 Step 1**：WS 鉴权 + 双订阅同时收到 + 跨账号隔离 + `since=0` replay+live 衔接 + 心跳超时 35s close

**通过判据**
1. `pnpm test:api` 一条命令跑完，全绿，时长 < 60s（含 alembic upgrade × N）。
2. 故意把 `routers/providers.py` 的 `WHERE user_id = ?` 删掉 → 账号隔离 case 红 → 读 stdout 能立刻定位是哪条 assertion 失败、传入参数是什么。
3. 故意把心跳间隔从 25s 改成 100s → 心跳超时 case 在 35s 超时窗内仍红（assert 不通过），证明 case 不依赖具体实现细节摆烂。
4. CI-friendly：`pnpm test:api` 退出码 = 失败数（成功 0），生成 `tests/.results/api.junit.xml` 供后续 CI 解析。

---

### Phase 4 — Playwright 脚手架与核心 helper

**做什么**
- 依赖：`@playwright/test`, `pg`（直查 Postgres）, `sirv-cli`（serve build）。
- `playwright.config.ts`：
  - `projects` = 各 profile，每个 project 自己的 `webServer`（指向对应 cache build 的 sirv）+ 共享 `globalSetup` 起 backend 9011。
  - `reporter`: `[['list'], ['json', { outputFile: 'tests/.results/e2e.json' }], ['html', { open: 'never' }]]`。
  - `use.trace = 'on-first-retry'`、`use.screenshot = 'only-on-failure'`、`use.video = 'retain-on-failure'`。
- 在 dev/test 构建里给 `window` 暴露调试钩子（受 `EXPOSE_DB=true` env 守卫，prod build 不打开）：
  - `window.__db__ = db` —— 给 Playwright `page.evaluate` 用
  - `window.__authSource__ = authSource` —— 强制登录态切换
  - 受 env 守卫的代码进 `src/boot/expose-debug.ts`，不污染业务路径。boot 文件首行 `if (String(process.env.EXPOSE_DB) !== 'true') return`。
- helpers 实现要点：
  - **`auth.ts`** —— `loginViaUI(page, user)`（走 AccountPage 真实登录，给 stage1.5 自身验证用）+ `loginViaApi(page, user)`（直接调 `/auth/login` + `localStorage.setItem` + reload，给其他 spec 快速 setup 用）。两条路径都要保留。
  - **`tabs.ts`** —— `openTabs(browser, user, n)` 用 `storageState` 复用登录态；`openTabsForUsers(browser, [u1, u2, ...])`。
  - **`db.ts`** —— 用 `page.evaluate` 走 `window.__db__.<table>`；提供 `dumpTable(page, name)` / `maxVersion(page, name)` / `clearAll(page)` / `putRow(page, table, row)`。
  - **`backend.ts`** —— 薄封 fetch，绕过 UI 直接打 9011 种数据；常用于"B 离线时 A 在干嘛"的快速 setup。
  - **`pg.ts`** —— `pgQuery(sql, params)`、`expectRowExists(table, id, userId)`、`countByUser(table, userId)`。
  - **`ws.ts`** —— `captureWs(page)` 返回累积所有帧的句柄，提供 `waitForFrame(predicate, {timeout})`、`expectFrame(predicate)`。
  - **`net.ts`** —— `setOffline(context, bool)`、`blockWS(context)`（route 拦截 upgrade）、`blockHost(context, hostname)`（用于切 dexie cloud SaaS）。
  - **`sync.ts`** —— `expectRowSync(pageA, pageB, table, id, {within: 1000})` 内部 poll `dumpTable`，是 Stage 2-4 双 tab 测试主力。
  - **`io.ts`** —— `exportData(page)` 触发现有 ExportDataDialog 按钮 + 等下载完成 + 返回临时文件路径；`importData(page, filePath)` 走 `setInputFiles`。
- `tests/e2e/smoke.spec.ts`：
  1. **baseline profile**：能开页 + 见首页基础 UI + 能在 Console 跑 `__db__.workspaces.toArray()`。
  2. **providers-rest profile**：能注册 backend 账号 + 拿到 access token + 调 `backend.ts` 写一个 provider + UI 能看见。

**通过判据**
1. `pnpm test:e2e --project=baseline -g smoke` 全绿，总时长 < 30s（含 build cache 命中 + sirv 起 + backend 起）。
2. 故意把后端 `/auth/login` 改返回 500 → smoke 红，stdout 能看到 `Expected status 200, got 500` 而不只是堆栈。
3. 跑 `pnpm test:e2e` 不带 `--project` → 跑全部 profile 全部 spec；本 Phase 落地时全部 profile 至少一个 smoke 全绿。

---

### Phase 5 — Stage 2 Step 4 验收 spec（首组真实 spec）

**做什么**
- `tests/e2e/stage2/step4-providers-realtime.spec.ts`，对应 cloud-sync-migration plan Stage 2 Step 4 的 4 条判据：
  1. **WS 双 tab 实时联动**：`realtime-ws` profile，A 经 UI 创建 / 修改 / 删除 provider，B `expectRowSync(...within: 1000)`。
  2. **离线 30s 重连补漏**：B `setOffline(true)` 30s，期间 A 经 `backend.ts` 直接写 3 次（增/改/删），B 恢复网络后 1.5s 内 dumpTable 与 server 状态一致。
  3. **`REALTIME_TRANSPORT` 空时退化**：`providers-rest` profile（无 ws），A 改后 B 不 reload 看不到，reload 后能看到 —— 等同 Stage 1 行为。
  4. **flag 全关字节级一致**：`baseline` profile，整个 spec 期间没有任何 9011 请求 / 没有任何 ws 帧，UI 行为等同 Stage 0 dexie 路径。

**通过判据**
1. 4 个 case 在对应 profile 全绿。
2. Step 4 代码尚未实现时跑这一文件 → 至少 case 1/2 应该红（说明判据真在卡 Step 4 的功能）；case 3/4 应该绿（说明判据正确隔离了"flag 关时不应破坏"）。
3. 运行命令简洁：`pnpm test:e2e -g step4` 一把搞定。

---

### Phase 6 — 反向回填 + plan 协同

**做什么**
- 把 cloud-sync-migration 已通过的 Step 判据补成自动 case，建立回归底盘：
  - **Stage 1 Step 7** 的 4 场景（清缓存恢复 / 跨设备同步 / flag 关回滚 / 双向兼容）
  - **Stage 1.5** 的 4 场景（账号隔离已在 Phase 3 落地，这里补 UI 端登录 / refresh / logout 链路）
  - **Stage 2 Step 1**（已在 Phase 3）
  - **Stage 2 Step 2 / Step 3** 的浏览器 case（之前跳过的场景 B/C：主动断网重连 + token 4001 + refresh + 重连）
- 在 `plans/cloud-sync-migration.md` 每个 Step 的「通过判据」段末尾追加 `- spec: <path>::<test name>` 或 `- api: <path>::<test name>`，让判据可追溯。
- 写 `tests/README.md`：怎么跑 / 怎么加 stage spec / helper 一览 / 失败排查指引。

**通过判据**
1. `pnpm test:api && pnpm test:e2e` 一条命令一把全绿，总时长 < 10min。
2. README 写完后另一个会话的 Claude 仅靠 README 能为 Stage 3 第一张表（reactives）独立写 spec，不再追问助手 API。
3. cloud-sync-migration plan 里每个已完成 Step 都至少有一行 `spec:` / `api:` 引用。

---

### Phase 7 — 未来 Stage 扩展点（设计预留，本次不实现）

下面这些**不在本次脚手架交付里**，但 Phase 4 helper 设计已为它们留好接口，避免日后重构：

- **Stage 3 叶子表（`reactives` / `avatarImages` / `installedPluginsV2` / `assistants`）**：每张表 spec 30 行级别，复用 `expectRowSync` + `pg.expectRowExists`。`reactives` 特殊（KV 形态）需小封 `repos.reactives.observeOne(key)` 的对应 helper。
- **Stage 4 级联**：新增 `helpers/cascade.ts`：`deleteWorkspaceWithChildren(page, wsId)` + `expectAllChildrenGone(pg, wsId)`。大数据 case 用 `backend.seed(table, N)` 直接灌 1 万行，不走 UI。
- **Stage 5 摘 dexie-cloud**：用 `browser.newContext()` 模拟"全新设备首次登录"；导出/导入用现成 `io.ts`；构建产物 grep 走 pytest 加 `tests/api/test_bundle_no_dexie_cloud.py`，读 `tests/.builds/<profile>/.../assets/*.js`。
- **现有用户数据迁移**：新增 `helpers/migration.ts`：`seedDexieData(page, table, rows)` + `triggerMigration(page)` + `expectMigrationStatus(client, table, expected)`。
- **跨版本导出/导入兼容**：把 master 分支某 commit 单独 build 一次存 `tests/legacy-build/<rev>/`，"新版导出 → legacy 导入" 与 "legacy 导出 → 新版导入" 各一个 case，校验数据完全一致。

---

## 与 cloud-sync-migration plan 的协同

- 两份 plan 平行维护、互不替代。
- cloud-sync-migration 每个 Step 的「通过判据」段，从 Phase 6 起新增 `spec:` / `api:` 行，引用本 plan 落下来的 case 路径。判据文档化即可执行化。
- 本 plan 的 Phase 5/6/7 引用 cloud-sync-migration 的 Stage 提供具体待测内容。
- 进度快照分别独立维护：本 plan 的快照只跟 Phase 进度，不 mirror cloud-sync 进度。
- 紧急情况（Phase 阻塞 cloud-sync 推进）允许本 plan 先标 ⏸️ 等回头再补，但**每个新 Step 必须在 Step 落地的同一 PR 里同步落 spec**，否则就走老路、回归累积失败。

---

## 风险

- **构建 cache 失效面**：`build-frontend-profile.ts` 的 cache key 仅含 env + git rev + package.json，未含 `quasar.config.js` / `uno.config.ts` / `src-pwa/` 等会影响构建产物的文件。**缓解**：cache miss 没数据正确性问题（顶多多 build 一次），先用最小 key，遇到 stale build 再扩。
- **`window.__db__` 暴露面**：dev/test build 才挂，但若有人误把 `EXPOSE_DB=true` 带进生产 .env，会暴露 IndexedDB 操作面。**缓解**：boot 文件加 `console.warn` + 在 prod docker build 流程里 `assert process.env.EXPOSE_DB !== 'true'` 主动 fail。
- **Playwright 端口冲突**：本机正跑 dev (9005) + dev backend (9010) 时跑 e2e，5434/9011/9007 不冲突，但用户若开了别的服务占用 9007/9011 会失败。**缓解**：test-up.sh 启动前 `lsof` 检查并友好报错。
- **后端测试 alembic 全量重跑慢**：当前 1-2 个 migration 没事，Stage 3+ 多了之后可能 60s 超标。**缓解**：到时切到 schema-template 模式（先 upgrade 一份 template schema，per-test 用 `CREATE SCHEMA ... LIKE template` 复制）。
- **dexie-cloud SaaS 真实依赖**：Stage 1.5–4 双写窗口里某些路径会触达 `https://znm3rqzc8.dexie.cloud`。e2e 默认在测试 env 关 `DEXIE_DB_URL`，只测 backend 路径；要测 dexie auth 链路时单独开 profile，必要时用 `net.ts.blockHost` 模拟 SaaS down。
- **Helper 漂移**：助手语义随时间偏离 plan 词汇，spec 可读性退化。**缓解**：Phase 6 的 README 维护一张「helper ↔ plan 词汇」对照表，每次新 Stage 引入新词汇时同步更新。

---

## 关键文件清单（供后续 PR 直接定位）

- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/docker-compose.test.yml`（Phase 1 新增）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/playwright.config.ts`（Phase 4 新增）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/pytest.ini`（Phase 3 新增）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/scripts/{test-up.sh,test-down.sh,backend-start.sh,backend-stop.sh,build-frontend-profile.ts,serve-build.ts}`（Phase 1-2）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/api/conftest.py`（Phase 3）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/e2e/helpers/*.ts`（Phase 4）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/env/.env.test.*`（Phase 2）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/README.md`（Phase 6）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/boot/expose-debug.ts`（Phase 4，给 e2e 暴露 `window.__db__` / `window.__authSource__`，受 EXPOSE_DB 守卫）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/quasar.config.js`（Phase 4，把 `expose-debug` 加进 boot 列表，受 env 条件挂载）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/requirements.txt` 或 `requirements-dev.txt`（Phase 3，pytest deps）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/.gitignore`（Phase 1，加 `tests/.builds/` `tests/.results/`）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/package.json`（Phase 1 起追加 `test:*` script）
