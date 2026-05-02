# Tests

两层自动化测试体系，对应 `plans/test-infrastructure.md` 的设计。
后端走 **pytest** (`tests/api/`)；端到端走 **Playwright** (`tests/e2e/`)。
所有判据可通过 `pnpm test:*` 一把跑完，`plans/cloud-sync-migration.md`
每个 Step 的「通过判据」都映射到具体 case name。

---

## 1. 一分钟上手

```bash
# 一次性安装（当前 .venv + chromium-headless-shell）
pnpm test:api:install
pnpm test:e2e:install

# 跑全部
pnpm test:api          # ~50s, 33 case
pnpm test:e2e          # ~60s, 14 active case + 26 skipped (profile gate)

# 加过滤
pnpm test:api -k providers
pnpm test:api -m "not slow"
pnpm test:e2e -g step4
pnpm test:e2e --project=baseline -g smoke
```

退出码：`0` 全绿 / 非 0 失败数。reporter 输出在 `tests/.results/`。

清理：
```bash
pnpm test:backend:stop   # kill 9011 backend
pnpm test:down           # docker compose down -v，删 5434 + volume
```

正常工作流不需要每次清理 —— `test:up` / `test:backend:start` 都是幂等，`run-pytest.sh` / `run-playwright.sh` 会复用已经在跑的 stack。

---

## 2. 端口与隔离

测试 stack 与 dev stack **完全隔离**。任何测试脚本都不该读写 dev 用的端口。

| 用途 | dev | 测试 |
|---|---|---|
| Postgres | 5433 (volume `aiaw-postgres`) | 5434 (volume `aiaw_test_pg`) |
| Backend (FastAPI) | 9010 | 9011 |
| 前端 SPA | 9005 / 9006 (PWA) | 9007 (baseline) / 9008 (providers-rest) / 9009 (realtime-ws) |

profile 是 flag 组合，每 profile 一份独立 `quasar build`：
- **baseline** — 全 flag 关，行为等同 Stage 0 (Dexie-only)
- **providers-rest** — backend providers 接通 + `BACKEND_AUTH=true`，无 realtime
- **realtime-ws** — 上面 + `REALTIME_TRANSPORT=ws`

`process.env.*` 是构建期内联 → 不能 runtime 切 flag → 每 profile 必须独立 build。
`build-frontend-profile.mjs` 按 `(env-sha256, git rev, package.json hash)` 联合 cache key 命中 `tests/.builds/<profile>/<key>/`，二次命中 < 1s。

---

## 3. 判据即代码

`plans/cloud-sync-migration.md` 每条「通过判据」都在对应 Step 段末尾写
`- api: tests/api/<file>::<test>` 或 `- spec: tests/e2e/<path>::<test>` 引用。
新增 Step 必须**同 PR 落 spec**，否则视为未完成。

当前判据映射快照（详细见 cloud-sync-migration plan）：

| Stage / Step | 判据来源 |
|---|---|
| Stage 1 / Step 1 健康检查 | `api: tests/api/test_health.py` |
| Stage 1 / Step 2 providers REST | `api: tests/api/test_providers.py` (7 case) |
| Stage 1.5 自家 JWT 鉴权 | `api: tests/api/test_auth.py` (13 case) + `spec: tests/e2e/stage1_5/auth-ui.spec.ts` |
| Stage 2 / Step 1 WS broker | `api: tests/api/test_realtime_ws.py` (11 case) |
| Stage 2 / Step 3 客户端 RealtimeConn | `spec: tests/e2e/stage2/step3-realtime-recovery.spec.ts` (scenarioB / C) |
| Stage 2 / Step 4 接 RemoteSyncSource | `spec: tests/e2e/stage2/step4-providers-realtime.spec.ts` (4 case) |

---

## 4. 已知预期红（spec-first）

| Spec | Case | 红的原因 | 转绿条件 |
|---|---|---|---|
| `step4-providers-realtime.spec.ts` | `case1 ws double-tab` | Stage 2 Step 4 未实现：`providers.server.ts.observeList()` 还没接 `RemoteSyncSource` | cloud-sync-migration plan Stage 2 Step 4 落地 |
| `step4-providers-realtime.spec.ts` | `case2 ws reconnect catch-up` | 同上 | 同上 |

这两个是 `plans/test-infrastructure.md` Phase 5 的 spec-first 设计 ——
代码先写 spec，红的输出本身是 Step 4 「未做」的实证。其它 case 全绿。

---

## 5. 加新 spec 的最短路径

### 5.1 后端 pytest

1. 在 `tests/api/` 新建 `test_<feature>.py`
2. fixture 全部从 `conftest.py` import — 不要手写 register / 直连 DB / 拿 token，下面的列表已经覆盖
3. 跑：`pnpm test:api -k <feature>`

`conftest.py` 提供：

| Fixture | 说明 |
|---|---|
| `_backend_up` | session autouse，9011 不通直接 `pytest.exit` |
| `db_reset` | function autouse，TRUNCATE refresh_tokens + users + providers + 重置 global_change_seq |
| `pg_conn` | 同步 psycopg 连 5434，用于 `SELECT * FROM ... WHERE ...` 直查 |
| `register_user` | factory：`await register_user()` 返新账号 dict (`id, email, password, access_token, refresh_token, user`) |
| `user_a` / `user_b` | 现成两个独立账号 |
| `client_a` / `client_b` | `httpx.AsyncClient` 预带各自 bearer |
| `anon_client` | 无 auth 的 `httpx.AsyncClient` |
| `ws_connect` | async ctxmgr：`async with ws_connect(token) as ws:` 自动带 `bearer.<token>` 子协议 |

注意点：
- email 域用 `@example.com`（`.local` / `.test` 被 `email-validator` 当 special-use 拒）
- access token 在同一秒签发会字节相同（HS256 over `(sub, iat-int, exp-int)`），不要 assert 两次 access token 必须不等；refresh token 是随机 opaque，可以
- 长 case 用 `@pytest.mark.slow`，`pnpm test:api -m "not slow"` 跳过

### 5.2 端到端 Playwright

1. 在 `tests/e2e/<stage>/` 新建 `<feature>.spec.ts`
2. import helper 而不是写新工具：`tests/e2e/helpers/` 已有 10 份
3. 决定 profile：在 `test()` 第一行 `test.skip(testInfo.project.name !== '<profile>', '<profile> only')`
4. 跑：`pnpm test:e2e -g <feature>`

**helper ↔ plan 词汇**对照（保持一致才能让 spec 短小）：

| Plan 用法 | helper |
|---|---|
| 「注册账号 A」 | `registerViaApi(email, pwd)` → TokenPair |
| 「A 登录」（程序化）| `loginApi(email, pwd)` 或 `loginViaApi(page, email, pwd)` |
| 「A 在 UI 登录」 | `loginViaUI(page, email, pwd)`（仅 Stage 1.5 spec 用） |
| 「跨账号 / 跨设备」（双 context）| `openContextsForUsers(browser, n)` — same-context 双 tab 共享 IDB+BroadcastChannel，会绕开 server fan-out |
| 「同一用户多 tab」 | `openTabsForUser(browser, n)` |
| 「把 token 注入页面」 | `injectAuth(page, pair)`（addInitScript 写 `aiaw.backendAuth.refresh` + `...user`）|
| 「等 IDB 暴露」 | `exposeReady(page)`（等 `window.__exposeDebugReady__`） |
| 「读表」 | `dumpTable<T>(page, 'providers')` / `getRow<T>(page, table, id)` |
| 「server 直接写」 | `putProvider(token, id, data)` / `listProviders(token, since)` / `deleteProvider(token, id)` |
| 「服务端真值」 | `pgQuery<Row>(sql, params)` / `expectRowExists(table, id, userId)` / `countByUser(table, userId)` |
| 「双 tab 在 1.5s 内一致」 | `expectRowSync(pageA, pageB, table, id, { withinMs: 1500 })` |
| 「等到 version >= N」 | `waitForVersion(page, table, id, minVersion)` |
| 「断网」 | `setOffline(ctx, true)` |
| 「禁 ws」 | `blockWS(ctx)` |
| 「禁 dexie SaaS」 | `blockHost(ctx, 'znm3rqzc8.dexie.cloud')` |
| 「捕 ws 帧」 | `captureWs(page)` → `waitForFrame(predicate, timeoutMs)` / `expectFrame(predicate)` |
| 「导出 / 导入」 | `exportData(page)` → 文件路径 / `importData(page, filePath)` |

### 5.3 Stage 3 第一张表：`reactives`

举例（从这份 README 即可下手）：

1. 后端：拷 `routers/providers.py` → `routers/reactives.py`，模型加 KV-shape
   字段（key 主键 + value JSON）；写 alembic migration；在 `app.py` 的
   `_enable_backend_data_api()` 里挂路由
2. 前端：抄 `providers.server.ts` → `reactives.server.ts`，在
   `repositories/index.ts` 加 SERVER_CAPABLE_TABLES
3. pytest：在 `tests/api/test_reactives.py` 复用 conftest 全部 fixture，
   PUT/list/get/since/soft-delete/account-isolation 6 case + unauth 401（同
   `test_providers.py` 形状）
4. e2e：在 `tests/e2e/stage3/reactives.spec.ts` 写 4 case：
   - `case1 cache write-through` — `pageA.evaluate(repos.reactives.put({...}))` →
     `dumpTable(pageA, 'reactives')` 含写入 → `expectRowExists('reactives', id, userId)`
   - `case2 cross-tab realtime`（仅 realtime-ws profile）— A put → B 1.5s 内看到
   - `case3 providers-rest fallback` — 同 step4 case3 形状
   - `case4 baseline byte-identical` — 抄 step4 case4
5. 把 spec 路径写回 cloud-sync-migration plan 对应 Step 段尾的
   `- api:` / `- spec:` 行

---

## 6. 调试与失败排查

| 现象 | 看哪 |
|---|---|
| `pnpm test:e2e` 报 9011 不通 | 后端没起：`pnpm test:backend:start`；或检查 `tail -f tests/.results/backend.log` |
| `pnpm test:api` 第一句 `pytest.exit` | 同上 |
| Playwright 红 + screenshot/video | `tests/.results/e2e-output/<spec-slug>/{test-failed-*.png,video.webm,trace.zip}`；trace 拿 `pnpm exec playwright show-trace tests/.results/e2e-output/<...>/trace.zip` 看 |
| HTML reporter | `tests/.results/e2e-html/index.html`，`open` 即可 |
| pytest junit | `tests/.results/api.junit.xml`（CI 用） |
| 想跑单 case 调试 | `pnpm test:api -k <substring> -x -s` / `pnpm test:e2e -g <pattern> --headed` |
| Playwright UI 模式 | `pnpm exec playwright test --ui` —— `webServer` 仍由 run-playwright.sh 起，所以先 `bash tests/scripts/run-playwright.sh --list` 让它准备好 build 缓存，再开 UI 模式跑（UI 模式不会自动跑 build） |
| 改了 .env profile 但 cache 命中旧 build | cache key 含 env-file-sha256，改 env 自动 miss；如果只改 quasar.config.js / uno.config.ts 不在 key 里，需要 `rm -rf tests/.builds/<profile>` 强制 rebuild |
| 改了 backend Python 但行为没变 | `pnpm test:backend:stop && pnpm test:backend:start` —— uvicorn 没开 reload |
| `dexie-cloud-addon` 真去 SaaS | 确认 `.env.test.<profile>` 里 `DEXIE_DB_URL=` 是空的；要测 dexie auth 必须显式新建 profile |
| 测试副作用残留 | `pnpm test:down` 删 volume（含 alembic 历史）；下次 `test:up` 自动 alembic upgrade head |

---

## 7. 守则（出问题前先想想）

- **判据失败优先修代码**，不要为让红变绿就去改 spec 的预期值
- **新 Step 必须同 PR 落 spec** —— 否则技术债累积、cloud-sync 节奏被回归覆盖度卡住
- **flag 默认关 = 字节级一致** —— 任何加入 `BACKEND_DATA_TABLES` 的表，都要在
  baseline profile 跑一份「零 9011 请求 + 零 ws 连接」case（参考 step4 case4）
- **构建产物里禁止泄漏 `EXPOSE_DB=true`** —— Dockerfile 已有 `grep` 守卫主动 fail，
  不要手动绕过
- **测试不连 dev 的 DB / backend** —— 5433 / 9010 不在测试脚本里，断言里也别有
- **同一 BrowserContext 双 tab 不验 realtime** —— 共享 IDB + BroadcastChannel
  会让 A 的本地 put 不经 server 就在 B 里出现，必须用 `openContextsForUsers`
- **`page.reload()` + `addInitScript`** —— addInitScript 在每次 navigation 都重跑
  会把 localStorage 重置成最初注入的（已被 boot 消费、轮换过的）token，
  reload 前先 `injectAuth(page, await freshSession(user))`
- **`dispatchEvent('click')` 而不是 `.click()`** —— Quasar 偶尔把 transient
  q-dialog 挂在主页上拦截 pointer events，dispatchEvent 直接触发 Vue handler
  跳过 actionability；只在确实被拦截时用，不要默认 force
- **测试运行期间不要 `pnpm dev`** —— dev 也跑 vite，端口虽不冲突但 disk I/O
  会拖慢 quasar build cache miss 路径
