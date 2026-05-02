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
pnpm test:api          # ~50s, 38 case
pnpm test:e2e          # ~80s, 17 active case + 85 skipped (profile gate, 6 profiles)

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
| 前端 SPA | 9005 / 9006 (PWA) | 9007 (baseline) / 9008 (providers-rest) / 9009 (realtime-ws) / 9012 (realtime-sse) / 9013 (realtime-poll) / 9014 (realtime-auto) |

profile 是 flag 组合，每 profile 一份独立 `quasar build`：
- **baseline** — 全 flag 关，行为等同 Stage 0 (Dexie-only)
- **providers-rest** — backend providers 接通 + `BACKEND_AUTH=true`，无 realtime
- **realtime-ws** — 上面 + `REALTIME_TRANSPORT=ws`
- **realtime-sse** — 上面 flag + `REALTIME_TRANSPORT=sse`（SSE 单向流降级）
- **realtime-poll** — 上面 flag + `REALTIME_TRANSPORT=poll`（5s 轮询，最低保证最终一致）
- **realtime-auto** — 上面 flag + `REALTIME_TRANSPORT=auto`（先试 ws → sse → poll，运行时降级）

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
| Stage 2 / Step 5 SSE / poll / auto 降级 | `api: tests/api/test_realtime_sse.py` (5 case) + `spec: tests/e2e/stage2/step5-transport-degradation.spec.ts` (4 case) |
| Stage 3 / 批次-3a reactives KV | `api: tests/api/test_reactives.py` (8 case) + `spec: tests/e2e/stage3/reactives-realtime.spec.ts` (4 case) + `tests/e2e/stage3/reactives-cache-roundtrip.spec.ts` (2 case) + `tests/e2e/stage3/persistent-reactive-passthrough.spec.ts` (1 case) |

---

## 4. 已知预期红（spec-first）

当前无已知预期红。Phase 5 的 step4 case1 / case2 已随 cloud-sync-migration
plan Stage 2 Step 4 落地转绿（providers.server.ts 接到 RemoteSyncSource）。
`pnpm test:api && pnpm test:e2e` 一把全绿。

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
| 「server 直接写」（providers） | `putProvider(token, id, data)` / `listProviders(token, since)` / `deleteProvider(token, id)` |
| 「server 直接写」（reactives，KV） | `putReactive(token, key, value)` / `listReactives(token, since)` / `getReactive(token, key)` / `deleteReactive(token, key)` |
| 「服务端真值」 | `pgQuery<Row>(sql, params)` / `expectRowExists(table, id, userId)` / `countByUser(table, userId)` |
| 「双 tab 在 1.5s 内一致」（id 主键） | `expectRowSync(pageA, pageB, table, id, { withinMs: 1500 })` |
| 「双 tab 在 1.5s 内一致」（KV / 任意字段） | `expectKvRowSync(pageA, pageB, table, field, value, { withinMs: 1500 })` —— 给 reactives 这种用 `key` 而非 `id` 的表用 |
| 「等到 version >= N」 | `waitForVersion(page, table, id, minVersion)` |
| 「断网」 | `setOffline(ctx, true)` |
| 「禁 ws」 | `blockWS(ctx)` — 走 Playwright 1.48+ `routeWebSocket()`，匹配 `**/api/v1/stream**` |
| 「禁 sse」 | `blockSSE(ctx)` — `route('**/api/v1/stream/sse**', abort)` |
| 「禁 dexie SaaS」 | `blockHost(ctx, 'znm3rqzc8.dexie.cloud')` |
| 「捕 ws 帧」 | `captureWs(page)` → `waitForFrame(predicate, timeoutMs)` / `expectFrame(predicate)` |
| 「导出 / 导入」 | `exportData(page)` → 文件路径 / `importData(page, filePath)` |

### 5.3 Stage 3 第一张表：`reactives`（已落地，可作为模板）

`reactives` 是 KV 形状的第一张 backend 表（复合 PK `(user_id, key)` + envelope `{key, version, updated_at, deleted, data}`），落地物可直接参考：

- 后端：`src-backend/data/models/reactive.py`（PrimaryKeyConstraint + 共享 `global_change_seq`）+ `routers/reactives.py`（envelope 用 `key` 替 `id`）+ alembic migration `d6a3f8c91e22` + `stream.py`/`sse.py` `TABLE_MODELS` & `SERIALIZERS` 加 reactives + `app.py::_enable_backend_data_api` lazy import
- 前端：`src/data/repositories/reactives.server.ts`（KV unwrap：`db.reactives.put({key: row.key, value: row.data})`；**`get`/`put`/`delete` 都触发 `ensureRealtimeSubscription`**——`persistent-reactive.ts` 用 `useLiveQuery(get(key))` 而非 `observe*`，realtime 必须在所有读写路径上都激活，否则跨 tab 推送拿不到）
- env：`.env.docker` `BACKEND_DATA_TABLES=providers,reactives`；测试 profile 同步加（baseline 仍空）
- 测试：
  - `tests/api/test_reactives.py` 8 case（CRUD + ?since 严格大于 + soft-delete + delete-then-put 复活 + 同 key 跨用户隔离 + unauth 401）
  - `tests/e2e/stage3/reactives-realtime.spec.ts` 4 case（ws double-tab / 30s reconnect / providers-rest no-realtime / baseline byte-identical）
  - `tests/e2e/stage3/reactives-cache-roundtrip.spec.ts` 2 case（clear+reload bootstrap / cache 不重 ?since=0）
  - `tests/e2e/stage3/persistent-reactive-passthrough.spec.ts` 1 case（put → server 真值 → 第二 tab observeOne+get → realtime 更新）

模板复用要点（写 批次-3b 等其他叶子表时）：

- KV 表 spec 用 `expectKvRowSync(pageA, pageB, table, 'key', value)` 而不是 `expectRowSync`（id 主键表用后者）
- 表名要进 `stream.py` / `sse.py` 的 `TABLE_MODELS` + `SERIALIZERS`，否则 client subscribe 收 `unknown-table` 错误，realtime spec 全红——这是 Stage 3 第一次掉过的坑
- 测试用 conftest 的 `db_reset` 必须把新表名加进 TRUNCATE 列表（`tests/api/conftest.py`），不然测试间脏数据相互干扰

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
