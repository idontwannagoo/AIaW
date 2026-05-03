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
pnpm test:api          # ~74s, 96 case
pnpm test:e2e          # ~4.7min, 63 active case + 224 skipped (profile gate, 6 profiles)

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
| 前端 SPA | 9005 / 9006 (PWA) | 9007 (baseline) / 9008 (providers-rest) / 9009 (realtime-ws) / 9012 (realtime-sse) / 9013 (realtime-poll) / 9014 (realtime-auto) / 9015 (import-job) |

profile 是 flag 组合，每 profile 一份独立 `quasar build`：
- **baseline** — 全 flag 关，行为等同 Stage 0 (Dexie-only)
- **providers-rest** — backend providers 接通 + `BACKEND_AUTH=true`，无 realtime
- **realtime-ws** — 上面 + `REALTIME_TRANSPORT=ws`
- **realtime-sse** — 上面 flag + `REALTIME_TRANSPORT=sse`（SSE 单向流降级）
- **realtime-poll** — 上面 flag + `REALTIME_TRANSPORT=poll`（5s 轮询，最低保证最终一致）
- **realtime-auto** — 上面 flag + `REALTIME_TRANSPORT=auto`（先试 ws → sse → poll，运行时降级）
- **import-job** — 与 realtime-ws 同 flag 集（含 `BACKEND_DATA_TABLES` 全 10 张表 + `REALTIME_TRANSPORT=ws`），独立端口 + 独立 build cache slot 给 Stage 4.5 / Step 7+ 的 ImportJob multipart 上传 + Phase A-D end-to-end specs 用

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
| Stage 3 / 批次-3b assistants / installedPlugins / avatarImages | `api: tests/api/test_{assistants,installed_plugins,avatar_images}.py` (各 8 case) + `spec: tests/e2e/stage3/{assistants,installed-plugins,avatar-images}-{realtime,cache-roundtrip}.spec.ts` |
| Stage 4 硬前置 2 BlobStore + `/api/v1/blobs` | `api: tests/api/test_blobs.py` (27 case：CRUD + 64KB 边界 + 5MB roundtrip + 跨用户隔离 + presign sig 篡改/过期/缺失/未知 sha + delete ref + unauth 401 + LocalFS 分片路径) + `spec: tests/e2e/stage4_pre/blob-client.spec.ts` (6 case：putBlob round-trip + dedup + serializeAttachment 阈值 + materialize inline/ref + baseline guard) |
| Stage 4 硬前置 3 作用域过滤 + scoped pull | `api: tests/api/test_dialogs.py::test_list_with_workspace_id_filters_to_scope / test_workspace_id_combined_with_since_and_limit / test_workspace_id_account_isolation / test_no_scope_param_returns_full_user_table` + `api: tests/api/test_items.py::test_list_with_dialog_id_filters_to_scope / test_dialog_id_combined_with_since_and_limit / test_dialog_id_account_isolation / test_no_dialog_id_param_returns_full_user_table` + `spec: tests/e2e/stage4_pre/scoped-pull.spec.ts` (4 case：empty-IDB scoped only / 3-dialog switch / cache prevents redundant fetch / cross-dialog realtime doesn't pollute scope cache) |
| Stage 4 / 批次-4d artifacts | `api: tests/api/test_artifacts.py` (22 case：CRUD + workspace FK 422/409 + cross-user 409 + inline/ref versions round-trip + 64KB 边界 + workspace cascade + workspaceId scope 过滤 + cursor 三参数组合 + account isolation) + `spec: tests/e2e/stage4/artifacts-large.spec.ts` (6 case：ws 双 tab inline put/update/delete / ws 双 tab 100KB+ ref versions 字节级 A→B / ws 双 tab workspace cascade tombstones artifacts / scoped pull 仅目标 workspace 进 IDB + 网络仅 ?workspaceId=X / providers-rest no-realtime / baseline byte-identical) |
| Stage 4 / 批次-4e messages | `api: tests/api/test_messages.py` (23 case：CRUD（list 必带 dialogId）+ dialog FK 422/409 + cross-user 409 + inline/ref contents round-trip + 64KB 边界 + dialogId mandatory 422 + cursor 三参数组合 + cross-dialog isolation + dialogId account isolation + dialog cascade messages + workspace 二跳 cascade messages + cascade scoping isolation) + `spec: tests/e2e/stage4/messages-attachment.spec.ts` (7 case：ws 双 tab inline put/update/delete / scoped pull 仅 dialogId scope + 零 bare GET /messages / ws 双 tab streaming 200 PUTs converge with monotonic versions on B / message-stream-flush 三触发 4 sub-assertions（200ms 时间窗 + 1KB byte threshold + sentence boundary + stop() finalize） / ws 双 tab cascade workspace→messages tombstones / providers-rest no-realtime + reload+scoped find recovers / baseline byte-identical) |
| Stage 4.5 / Step 1 ImportJob 模型 + worker + Phase A | `api: tests/api/test_import_job.py` (8 case：Phase A row-count 提取 + 流式内存 < 200MB（slow，~67MB fixture，实测 RSS delta ≈ 72MB << budget）+ 损坏 JSON 标 failed + 不支持 formatName 标 failed + 同 user 第二 active job IntegrityError + terminal 不参与唯一约束 + worker 主循环吃 status='parsing' 推到 phase_b + envelope wire 形态 server-routed table 契约) |
| Stage 4.5 / Step 2 multipart 上传 5 endpoint + LocalFs `_internal` PUT shim | `api: tests/api/test_imports_router.py` (12 case：create job 返 job_id+upload_id+raw_object_key + part-url HMAC sig 64-hex + expires_at ±10s 窗 + complete 全 part 推进 worker queued→phase_b/c + missing parts 三 sub-case `contiguous`/`start at 1`/`duplicate` 400 + GET status snapshot 全字段 + GET `?status=active` 返 active 单 job、cancel 后空、bad status 400 + DELETE cancelled + 立即可建新 job（slot 释放）+ cross-user 404-mask（GET/parts/complete/DELETE 4 path 全 404，绝不 403）+ wrong-state part 409 + 第二次 create 409 + winner snapshot + 篡改 sig 403 'bad signature' / 过期 exp 403 'url expired' + trust-the-hash dedup 跨 user 上传同字节 canonical 单文件) |
| Stage 4.5 / Step 3 Phase B 结构表写入 + imported_from_job_id 加列 + DELETE cascade | `api: tests/api/test_import_phase_b.py` (14 case：workspaces UUID 原样写回 + 7 张表 dependency-order LWW UPSERT + LWW skip-older + LWW overwrite-newer + 行 tagged with job_id+timestamp + 进度事件 indirect proof（processed_rows+status=phase_c）+ phase_b→phase_c 状态转移 + 缺表 NDJSON ok + tmp 目录缺失→failed crash recovery + dexie camelCase→PG snake_case 表名映射 + reactives KV-PK 复合 PK 写入 + DELETE cascade 7 表 14 行软删共享 cascade_version + DELETE 后 WS subscribe 抓 1 workspaces + 5 dialogs delete event + alembic migration 加列 + FK ON DELETE SET NULL + 索引 三档 information_schema 校验) |
| Stage 4.5 / Step 4 Phase C messages 文字写入 + `_pending_blob_extraction` 标记 + dead_letter array-concat | `api: tests/api/test_import_phase_c.py` (14 case：1500 行 / 500 行/批 + processed_rows 累计 + PHASE_C_BATCH_SIZE 静态断言 + size 64KB / envelope 双 prong 标记 + recursive envelope 深嵌套扫 + size unit test + orphan FK 入 dead_letter 含 `error: 'orphan: dialog ... not found'` + 25ms 高频 poll 抓 ≥3 distinct progress checkpoints batch_size 倍数对齐 + phase_c→phase_d→done 状态转移（Step 5 落地后 reach done）+ LWW message text skip 旧版 + 0/500/501/1499 行四档 batch boundary + 同 ndjson 二跑 LWW WHERE FALSE 路径 idempotent + partial index ix_messages_pending_blob_extraction WHERE \_pending\_blob\_extraction=TRUE pg_indexes 校验 + imported_from_job_id 全行打标 + dead_letter JSONB array-concat 长度 2 + 顺序保留) |
| Stage 4.5 / Step 5 Phase D attachments → 对象存储 | `api: tests/api/test_import_phase_d.py` (13 case：< 64KB inline 留存 / ≥ 64KB → ref envelope + BlobStore 文件 + blob_refs 行 / 同 sha dedup（call_count=2 unique=1）/ 4 attempts 重试后 dead_letter（envelope 留 inline + flag cleared）/ done 后 tmp dir + raw upload 清理 / 16 attachment + 0.1s sleep 验 peak 并发 = 4（slow）/ crash idempotent（recovery 不二次 put）/ progress 跨 batch_size=16 边界 publish + processed_blobs 单调 / `_walk_attachments` 深嵌套 path tuple 精确 / 5 个 1KB 全 inline 不调用 put / 单 row 多 attachment 中间失败不阻塞两侧 + dead_letter 单条 / actual_size 100KB 胜 declared_size=10 / `_PHASE_D_INLINE_MAX_BYTES == BLOB_INLINE_MAX_BYTES` 两端不漂移 grep 校验) + 新 helper `tests/api/helpers/blob_mocks.py`（`patch_put_with_counter` / `patch_put_with_delay` / `patch_put_always_fails` / `patch_put_fails_for_sha` 4 个 ctx-mgr）+ `fresh_engine_loop` async fixture（dispose data.db.engine 让 asyncpg pool 在新 loop 重建）|
| Stage 4.5 / Step 6 import_jobs 注册为 read-only realtime channel + 405 client-write reject | `api: tests/api/test_import_realtime.py` (8 case：端到端 multipart + WS subscribe 抓 status 序列 superset {queued, phase_b, done} + monotonic rev / envelope 形态契约 14 data keys 子集 + Phase B 注 `phase_b_table` 额外字段用 superset / B 端 2.5s 0 events leaked cross-user 隔离 / `GET ?status=active` cross-user 隔离 / PUT/PATCH/POST/DELETE 4 method 全 405 + 'read-only' + Allow / GET 路径 405 + 业务路径 200 / SSE channel 同模板支持 import_jobs / since=max_rev_seen reconnect 拿漏掉的 events 末尾 status='done')。复用 `tests/api/test_realtime_sse.py::SseReader` + `_open_sse` |
| Stage 4.5 / Step 7 前端 ImportDataDialog 重写 + multipart upload helper + AccountPage 迁移状态卡片 | `spec: tests/e2e/stage4_5/step7-import-flow.spec.ts` (6 case · profile import-job：① upload + close tab + reopen sees progress（fixture 5×1MB attachments / 跨 context 同 user / 等 composable.value.status==='done' 验 REST + WS 端到端连通）/ ② upload resumes after simulated network drop @slow（7MB / 2.5MB part = 3 parts / setOffline 中断 → cursor 留前缀 → setOnline 续传 仅 PUT 缺失 part / `seenPartUrls` 不含 cursor 部分）/ ③ cancel button aborts job and clears state（不 complete 让 job 停 'uploading' active 态 / Cancel 按钮 dispatchEvent('click') 绕 backdrop / 验 server status='cancelled' + cursor=null + Dismiss 出现）/ ④ phase B complete makes workspaces visible（pgQuery 验 server 真值）/ ⑤ phase C complete makes message text readable（pgQuery 验 3 messages text + `_pending_blob_extraction=false`）/ ⑥ phase D complete makes attachment renderable @slow（5×1MB attachments / pgQuery `blob_refs` ≥5 distinct sha + 5 messages 各 `data.attachment.type='ref'` + url/sha256 非空 + flag clear）)。fixture builder 内嵌 spec（buildDexieExport / buildPaddedFileInPage / page-context attachment generator），不需新 helper |
| Stage 4.5 / Step 8 `GET /api/v1/bootstrap` endpoint + 首屏 router guard | `api: tests/api/test_bootstrap.py` (11 case：8 keys 强约束 set + items/artifacts 显式不在 + 50/dialog cap + 1MB typical user + 1MB+5% 截断 dialog 整边界 @slow + account isolation + active import 不阻塞 + Cache-Control 'private, max-age=10' + envelope == list endpoint 字节级一致 + 401 unauth + items/artifacts 二次断言不污染 messages_recent envelope + empty dialog 不进 messages_recent) + `spec: tests/e2e/stage4_5/step8-bootstrap.spec.ts` (5 case · profile realtime-ws：fresh login 1.5s 内 IDB 含 seeded ws / 3s mock + 2s timeout fallback 写 sessionStorage flag + banner / 多次 navigation 仅 1 次 GET bootstrap / 3 ws seed → IDB 子集断言 / 500 error 同 fallback 路径 HttpError 分支)。新 helper `tests/e2e/helpers/bootstrap.ts`（5 export：mockBootstrapTimeout / mockBootstrap500 / clearBootstrapState / assertBootstrapFallback / readAttemptedFlag）|

---

## 4. 已知预期红（spec-first）

当前无已知预期红。Phase 5 的 step4 case1 / case2 已随 cloud-sync-migration
plan Stage 2 Step 4 落地转绿（providers.server.ts 接到 RemoteSyncSource）。
`pnpm test:api && pnpm test:e2e` 一把全绿，唯一 timing-flake 是
`step3-realtime-recovery.spec.ts::scenarioB ws auto-reconnect after offline window`
（`waitForState` 5s 内偶尔等不到 'open'→close transition），与 Stage 4 硬前置 2 工作无关。

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
| 「上传 blob / 反向 / 自动分流」（Stage 4 硬前置 2） | `(window as any).__blobClient__.putBlob(buf, ct)` / `.fetchBlob(ref)` / `.serializeAttachment(buf, ct)` / `.materializeAttachment(env)`——挂在 `window.__blobClient__` 由 `boot/expose-debug.ts` 暴露，需要 `EXPOSE_DB=true` profile + `BACKEND_DATA_API_URL` 设了才能 putBlob，否则 `serializeAttachment(>=64KB)` 会抛 |
| 「mock LocalFsBlobStore.put」（Stage 4.5 Step 5+） | `tests/api/helpers/blob_mocks.py`：`patch_put_with_counter()` / `patch_put_with_delay(seconds)` / `patch_put_always_fails(msg)` / `patch_put_fails_for_sha(bad_shas)` ——async ctx-mgr 风格，class-level 替 `LocalFsBlobStore.put`；只对 in-process 直调 `run_phase_d` 的测试有效（backend 9011 是另一进程不受影响）。需要 `fresh_engine_loop` async fixture 给每 case 重建 asyncpg pool 防 `Future attached to a different loop`。|

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
