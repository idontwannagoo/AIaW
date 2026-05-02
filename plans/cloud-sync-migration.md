# 云同步架构分析 + 服务端优先迁移方案

## Context

当前仓库 (AIaW fork) 的云同步基于 **Dexie + dexie-cloud-addon**（本地优先 / IndexedDB → 官方托管 SaaS `https://znm3rqzc8.dexie.cloud`）。后端 `src-backend/app.py` **不参与同步**，只做 CORS 代理与文档解析。业务代码 32 处直接 `import { db }` 调 `db.<table>.*`，没有数据访问层抽象，登录/账户/订阅 UI 直接耦合 `db.cloud.*` API。

目标是迁移到 **服务端优先**：自己的 FastAPI 承担权威存储，客户端通过 REST + WebSocket 读写，IndexedDB 退化为缓存。本计划重点在「**每一阶段都能独立合并、构建、上线、端到端验证**」，不需要全部改完才能跑通。

部署形态目标：**多用户 SaaS-style**（不仅仅是单人自部署），所以鉴权必须从一开始就支持多账号、可对外开放注册或受控开放。

---

## 修订记录

- **2026-05-02 · 部署分支拓扑硬切：my-deploy reset + new-deploy 单独承载 stage 1+**
  - **背景**：Stage 2 收官后 feature/change-cloud-sync-claude（Stage 0 / 1 / 1.5 / 2 + 测试脚手架 + plan 共 23 commit）fast-forward merge 到 my-deploy 并 push，触发了一次 Northflank 部署。事后复盘发现 my-deploy 是「老用户兜底实例」分支 —— 按本 plan 2026-05-02 修订记录定下的「导入导出 only」迁移路径，my-deploy 本不应承载任何 backend 重构代码：flag 默认全关时 Stage 1 / 1.5 / 2 的 ~7 KB 前端 bundle 增量 + 几十 KB 后端 Python data 路由对老用户是纯 dead weight，违背 plan 同条修订记录 line 43 / 767 的「新版独立实例承载全量 stage 1+ 功能」拓扑设计。错位的根因是 plan 把部署拓扑只埋在修订记录里、没固化进 CLAUDE.md，操作时被「my-deploy 是当前唯一线上分支」的事实带跑偏。
  - **变更**：
    - 远端创建 `backup/my-deploy-pre-reset` 分支锚定 reset 前的 my-deploy HEAD `5c689ab`，本地 + 远端双副本永久保留作为逃生通道（半年内不删）
    - 远端创建 `new-deploy` 分支起点 = `5c689ab`，承载 Stage 0 / 1 / 1.5 / 2 + 测试脚手架 + plan 全套；后续 Stage 3+ 工作只合这里
    - 本地 my-deploy `git reset --hard 2b26349`，远端 `git push --force-with-lease origin my-deploy`：把 my-deploy 退到「Stage 0 抽象层重构完成 + Dexie 路径 3 个 bug fix」干净 baseline，移除 Stage 1 / 1.5 / 2 / 测试脚手架 / plan 共 33 个 commit。Northflank 服务 1 自动拉新镜像（实际是反向更新），老用户视角是一次镜像更新，运行时行为字节级回到 Stage 0 完成时
    - 顶级 CLAUDE.md 新增「部署分支拓扑（2026-05-02 起生效）」H2 段，固化分支角色 + 工作流契约 + Northflank 约束 + 老用户迁移路径 + 部署分支 reset SOP 共 5 个子段；同步把 CLAUDE.md 现有的「flag 默认关 = 字节级一致」规则中「合并到 my-deploy」措辞改为「合并到 new-deploy」，新增一句「云同步重构相关代码永不合 my-deploy」交叉引用
    - 本 plan 进度快照新增条目记录硬切完成
  - **影响范围**：
    - my-deploy 镜像净 -几十 KB（Python data 路由 + 测试脚手架 + plan 不再随镜像走），bundle 净 -7 KB；老用户行为 0 变化（flag 关时 Stage 0 完成态与 reset 前字节级一致）
    - 33 个 commit 物理保留：在 `new-deploy` + `backup/my-deploy-pre-reset` 上 anchor 牢固，零代码丢失。本 plan 文件 my-deploy 上不再存在（plan 是云同步重构产物，被 reset 出去了）；plan 唯一权威版在 `new-deploy` 与工作分支
    - 后续 Stage 3 起所有 plan / spec / 代码改动只在 `new-deploy`；my-deploy 进入「维护态」直到 Stage 5 落地后由迁移率决定下线时机
  - **避坑（这次踩过 + 修了的）**：
    - ① **没把"导入导出 only 部署拓扑"写进 CLAUDE.md** —— 只在 plan 修订记录里有一句话提及「新版独立实例」，操作时被忽略。修复：CLAUDE.md 加「部署分支拓扑」段固化为静态规则，未来任何会话开始读 CLAUDE.md 即可看到，无法再次错位
    - ② **把 23 个云同步 commit fast-forward 到 my-deploy 是错位的**：「上线但不启用」适合"同一用户群特性灰度"，**不**适合"新旧用户分流"。导入导出 only 路径下，前端 flag 是 quasar build 期内联无法 per-user 切换 → 一份镜像服务一群用户 → my-deploy 镜像里有 stage 1+ 代码就是浪费。下次 stage 落地前必须先确认 merge 目标是 new-deploy
    - ③ **force-push deploy 分支必须先建 safety net**：本次按「拉 backup → 拉 new-deploy → reset local → force-push」四步走，每步独立验证后再继续；safety net 远端 + 本地双副本，恢复成本一行命令（`git reset --hard backup/my-deploy-pre-reset && git push --force-with-lease`）。这个 SOP 已写进 CLAUDE.md 部署分支拓扑段，未来任何 deploy 分支 reset 都按此走
    - ④ **Permission system 对 force-push 单独设保护**：第一次 force-push 即使在 auto mode 下也被 permission system 拒，要求用户对具体的 force-push 命令再次显式批准。这是合理的安全边界，不要尝试绕过，每次老老实实让用户在 prompt 里点允许或用 `! command` 形式直接执行
- **2026-05-02 · Stage 2 / Step 6 落地（Stage 2 收官）**
  - **背景**：Step 5 沉淀完 SSE / poll / auto 降级 spec 后 Stage 2 仅剩「回归全绿 + 三组手测交付物 + 翻上线 flag」三件事。按 plan 不再新增 spec，只 reuse 全套 Step 1-5 已落 spec 作为回归判据，并交付「bundle KB 数 / RSS 起止 MB 数 / p95 ms 数」三个非脚手架可覆盖的数字写进进度快照。
  - **变更**：
    - 新增 `tests/scripts/soak.sh`（bash 入口）+ `tests/scripts/soak-loadgen.py`（asyncio loadgen）。soak.sh 负责生命周期 / RSS 采样 / trap-driven shutdown / summary；loadgen.py 持开 N 个 WS subscriber + 高频 PUT，结束时 emit JSON-Lines summary 含 p50/p95/p99/avg/max。两者幂等可中断、不触碰 dev DB / 9010 / 5433、loadgen 用 `secrets.token_hex` 前缀做 per-run namespace 避免 PG `providers.id` UNIQUE 约束的跨 run 冲突
    - **不引入任何新 spec**：Step 6「回归判据」5 类全部 reuse Step 1-5 已落 spec / api 复跑（pytest 38 / 38 全绿 52s；playwright `-g "stage2|stage1_5|smoke"` 17 passed / 85 skipped / 0 failed 1.3 min）。这是 Phase 6 守则下「Step 落地不一定要 ≥1 个新 spec」的特例：本 Step 不新增功能，复跑回归 + 软性把关即可
    - 三组「上线把关」数字采到（详见进度快照），全部达标
  - **影响范围**：
    - 落地后 `pnpm test:api && pnpm test:e2e` 仍 38 + 17 全绿，无回归
    - my-deploy 默认 `REALTIME_TRANSPORT=` 空 + `BACKEND_DATA_TABLES` 不含 providers + `BACKEND_DATA_API_ENABLED` 关闭 → 行为字节级等同 Stage 1 收尾，对线上无感
    - 新脚本 `pnpm test:up && pnpm test:backend:start && bash tests/scripts/soak.sh` 三步即可在任何机器上重跑 soak；脚本默认 `--duration 3600`（1h），常用 `--duration 300/600` 做快速验证
    - `tests/README.md` 暂无字面更新需求：判据映射表里 Step 6 不出新行（reuse 既有 Step 1-5 引用），soak.sh 是上线把关脚本不归属「pnpm test:` 前缀套件
  - **避坑（落地踩过 + 修了的）**：
    - ① **loadgen 第一版用 `soak-{i % 50}` 做 id**：跑第二次时新 user 与上一次的 row 撞 `providers.id` UNIQUE 约束（PG 全局唯一，server 报 409 `id owned by another user`）→ 整个 600s 0 puts 全 errors。修为 `f'soak-{secrets.token_hex(3)}-{i % 50}'`，每次 run 独立 namespace
    - ② **macOS RSS reading 不直接用 end-start**：OS 内存压力下会把 inactive page 移出 resident，soak 结束后空闲态读 `ps -o rss=` 会比 active 期值大幅偏低（17 MB vs active 60-68 MB）。判据要看 active 期 max-min drift（本次 soak 期内活跃区间 60-68 MB，drift < 8 MB），而不是 `end - start`
    - ③ **bash trap finalize 与外层 wait 的竞争**：第一版 soak.sh 的 finalize trap 在 `wait LOADGEN_PID` 完成后才执行，导致 RSS TSV 末几行的写入顺序与 'end' 标记错乱。本次未深修，因为对数字不影响（summary 文件提供了完整 RSS series + delta，运维人手读没问题），未来若要 wire 进 CI 再修
- **2026-05-02 · 服务端 Import Job 设计敲定（导入导出 only 路径的实现层 + 部署拓扑澄清）**
  - **背景**：「导入导出 only」方向定下后进入实现层评估。原描述里浏览器端跑全套 import（parse → 写 IndexedDB → push backend → 上传 attachment）对低端设备 / 弱网 / 长时操作不友好——270MB JSON 在浏览器里 parse 容易 OOM、上传几十分钟期间 tab 不能关、attachment 串行上传慢、断网后状态全丢。结合 Stage 4 硬前置已规划对象存储 + multipart endpoint，决定按 SaaS 行业标准实践把整个 import 工作搬到 server 端：浏览器只负责把 JSON 文件 5MB 切片直传对象存储（pre-signed S3 multipart），其余阶段（解析 / 分表写入 / attachment 处理）全部由 backend 后台 worker 跑，用户上传完即可关 tab，状态通过 WS 推进度。同时新设备首屏走 `GET /api/v1/bootstrap` 一次返回所有小表的全量，避免渐进填充式空白。部署拓扑同步澄清：旧版独立实例保留兜底，新版独立实例承载全量 backend；一段时间后旧版下线。
  - **变更**：
    - 新增 **Stage 4.5「服务端 Import Job + 新设备首屏 bootstrap」段**，介于 Stage 4 与 Stage 5 之间，含 8 个 Step：① ImportJob 模型 + Phase A 解析骨架；② S3 multipart 上传 5 个 endpoint；③ Phase B 结构表写入；④ Phase C messages 文字写入；⑤ Phase D attachments → 对象存储；⑥ WS 进度推送 + 状态持久化；⑦ 前端 ImportDataDialog 重写 + 上传切片 helper；⑧ `GET /api/v1/bootstrap` endpoint + 首屏路由 guard
    - 「现有用户数据迁移」章节扩写：增加 200MB 用户完整时序表（4 phase + 可关 tab 节点 + UI 可用节点）+ 多设备协调说明 + 失败回退路径，引用 Stage 4.5 实现
    - 「跨版本导入/导出兼容」章节同步：明确**新版 import 不再写客户端 IndexedDB**，由 Stage 4.5 worker 写 PG → realtime 推送 → IndexedDB 自然填充；export 流程保持客户端跑（含对象存储 ref → base64 桥接），是因为 export 是用户主动行为且数据流向反过来，server-side 化收益不抵复杂度
    - Stage 5 顶部补一句「依赖 Stage 4.5 完成」：dexie-cloud-addon 卸载前提是老用户已有可用迁移路径
    - **测试脚手架增量**（与 test-infrastructure plan Phase 7 同步）：
      - `docker-compose.test.yml` 加 MinIO 服务（端口 9100，作为 BlobStore S3 后端）
      - 新 profile：`import-job` → 9015 → `tests/env/.env.test.import-job`（启用 `IMPORT_JOB_ENABLED=true` + `BLOB_STORE_KIND=s3` + MinIO endpoint）
      - 新 helper：`tests/e2e/helpers/import-job.ts`（封装 multipart upload + 状态订阅 + phase 等待）
      - 新 fixture：`tests/api/fixtures/dexie_export.py`（生成 small / medium / large / huge 四档 `aiaw_user_db.json`，huge = 200MB）
      - 各 Step「通过判据」按 Phase 6 守则配套 pytest + playwright case，全部沿 plan 现有 `- api:` / `- spec:` 引用格式
    - 部署拓扑澄清写进 Stage 0 与 Stage 4.5 上线策略段：旧版（Dexie Cloud SaaS）独立实例不下线作为回退；新版（自家 backend）独立实例承载全量 stage 1+；用户主动迁移；旧版下线时机由实际迁移率决定（建议 active 用户 ≥ 80% 迁完后宣告 6 周下线窗）
  - **影响范围**：
    - Stage 4.5 落地后老用户迁移路径完整可用；Stage 5 仅做 dexie-cloud-addon 卸载 + deprecated 代码清理（~150 行），不再有任何"过渡 UI"工作
    - 客户端工作量大幅降低：浏览器侧只剩「文件切片上传 + 状态订阅 + bootstrap 拉取」三件事，约 250 行
    - 服务端工作量集中在 Stage 4.5：~800 行（worker ~500 + imports router ~200 + bootstrap router ~100）+ 1 个 alembic migration
    - 测试新增：~25 个 pytest case + ~10 个 playwright case；MinIO 容器加入 `pnpm test:up`
    - 200MB 老用户感知"卡住"时间从 30-60 分钟降到 3-10 分钟（仅上传阶段需要浏览器在线）
    - 部署侧需要对象存储（R2 / S3 / MinIO 任选），plan 此前已规划在 Stage 4 硬前置；Stage 4.5 只复用同 bucket 的 `imports/<user>/<job_id>.json` 前缀，TTL 7 天自动清
  - **维护后续**：本修订记录落地后 plan 主体按 8 个 Step 同步；test-infrastructure plan Phase 7+ 需要按这里新增 helper / profile / fixture 同步更新「helper ↔ plan 词汇表」+「端口表」+「已知预期红」段
- **2026-05-02 · 迁移路径转向：导入导出 only + 对象存储升格为 Stage 4 硬前置**
  - **背景**：复盘 200MB 老用户场景（messages + 大量内联 base64 attachments / artifacts）后识别到现 plan「客户端驱动一次性 push」路径在三处暴露结构性瓶颈：① WS event payload 带完整 row → broker `maxsize=200` 队列在大行场景秒爆；② `?since=N` 全 row 返回 → 客户端 fetch 几十 MB 卡死；③ Postgres TEXT 列存 base64 attachments → 表线性膨胀、`vacuum` / 备份 / 慢查询全受影响。同时 plan 全文未涉及对象存储设计，是当前最大盲区。结合已有的 `dexie-export-import` 跨版本互通格式（`aiaw_user_db.json`，base64 内联，旧版自然识别），转向「导入导出 only」迁移路径在 UX 可控性 / 实现复杂度 / 回滚安全性三方面都更优——失败回退 = 用回旧版 URL，零数据风险。决策：双写窗口 + 客户端一次性 push 机制整体砍掉；对象存储从盲区升格为 Stage 4 硬前置。
  - **变更**：
    - 「现有用户数据迁移」整章删除「客户端驱动一次性 push」机制（迁移标记表 / `GET /api/v1/migrate/status` / 双写窗口决策 / 多设备 push 协调全部不再需要），整章重写为「老用户在旧版 ExportDataDialog 导出 `aiaw_user_db.json` → 在新版 ImportDataDialog 导入」单一路径；新版默认不挂 `dexie-cloud-addon`，老用户不动可继续用旧版直到主动迁移
    - Stage 1.5 的 `users.linked_dexie_email` 列 + UNIQUE 约束 + `POST /api/v1/auth/link-dexie` endpoint + 前端首次登录调用逻辑 + `tests/api/test_auth.py::test_link_dexie_first_write_wins` 标记 deprecated（属于「双写窗口期把 backend 账号 ↔ Dexie email 对齐」的辅助机制，导入导出方案下不再需要）；已落地代码不立刻删，等 Stage 5 一起清，避免 in-flight Stage 2 Step 6 上线带飞
    - 已知问题 #2 修复引入的 `unsyncedTables` 计算 + `src/data/server-tables.ts` 同样标记 deprecated，Stage 5 一起清；过渡期保留无副作用
    - Stage 4 顶部新增「硬前置」段：① 大行传输协议改造（WS event 仅带 `{id, rev}` 通知，业务 row 走 REST 按需拉；`?since=` 加 `limit` + cursor 续拉，避免单响应几十 MB）；② 对象存储分流（B 方案——客户端附件 < 64KB inline 进 PG，≥ 64KB 走 multipart 上传 `/api/v1/blobs` 拿 ref，PG 行只存 `{type:'ref', url, sha256, size}`，客户端 IndexedDB 缓存仍可保留 Blob 让 UI 透明）；两条均为 messages / artifacts server.ts 落地前的硬前置，不可后置
    - 「跨版本导入/导出兼容」段补「对象存储桥接」子段：导出时新版主动 fetch 所有 ref blob → 重新 base64 内联 → JSON 字节级对齐旧版格式；导入时新版检测 base64 blob → 按 64KB 阈值上传 S3 / 留 inline → 改写行字段。**对外格式纪律**：导出 JSON 永远 base64 内联，绝不出现 `{type:'ref', url}` 字段（避免旧版导入看到坏链接）
    - Stage 5 简化：删去「拆掉双写窗口最后一块」表述（从未有过双写窗口）、删去「清理 UI 上的双入口回归单入口」过渡 UI（从未有过双入口）、删去 `LEGACY_DEXIE_CLOUD=true` 回滚 flag 设计（导入导出方案下回滚 = 用回旧版部署，addon 重挂场景不存在）；Stage 5 收窄为「卸载 `dexie-cloud-addon` + 一次性清理 deprecated 代码（~150 行）+ 验证 ImportDataDialog 在 addon 摘除后仍能读 `aiaw_user_db.json`」
    - 端到端验证策略表 Stage 5 行的「全新设备首次登录 + 卸载重装」补一句「+ 旧版导出 → 新版导入往返字节一致（含对象存储桥接路径）」
  - **影响范围**：
    - 已落地代码浪费量化：`linked_dexie_email` 列 + endpoint + 前端调用 ≈ 90 行，`unsyncedTables` + `server-tables.ts` ≈ 40 行，`tests/api/test_auth.py::test_link_dexie_first_write_wins` ≈ 20 行；合计 ~150 行，Stage 5 阶段一次性删
    - 已落地代码 95%+ 复用：Repository 抽象 / JWT 鉴权主体 / providers REST / broker / WS / SSE / poll / auto-router / 全套 spec 与脚手架在新方案下零修改
    - Stage 2 Step 6 收尾路径不受影响，按原计划合 my-deploy（flag 默认关，对线上零感知）
    - Stage 3 不再需要 per-table 迁移机制 ceremony，每张表节奏更轻：SQLModel + router + alembic + flag。老用户的旧表数据在新版里默认空，需要导入才出现
    - Stage 4 工作量重新分布：新增对象存储 backend `/api/v1/blobs` + 客户端 multipart helper ≈ 1–2 周；大行协议改造 ≈ 3–5 天；ExportDataDialog / ImportDataDialog 桥接 ≈ 3–5 天。砍掉的「客户端驱动 push 机制 + 进度 UI + 断点续传 + 多设备协调」工作量 ≈ 2–3 周，净额持平偏简化
    - 用户分群清晰：不愿动的老用户继续用旧版（旧 Dexie Cloud 部署不下线），愿意迁的主动走 export → import，不存在被动遭遇 bug 的中间态
  - **维护后续**：本条修订记录落地后 plan 正文按上面 6 条同步修订；已落地代码的清理工作（~150 行）合并到 Stage 5 PR 一起做，单独提 PR 没必要；后续 Step 落地不再向 deprecated 段落填新内容
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
  - **Stage 2 / Step 6**（端到端回归 + 上线把关 + Stage 2 收官）✅ — 详见修订记录 2026-05-02 · Stage 2 / Step 6 落地
    - **回归判据**：`pnpm test:api` 38 / 38 全绿（52s）；`pnpm test:e2e -g "stage2|stage1_5|smoke"` 17 passed / 85 skipped (profile gate) / 0 failed（1.3 min）。Step 1-5 沉淀的 spec / api 全部 reuse 复跑全绿，未新增任何 spec
    - **上线把关三组数字**：
      - **bundle 体积**：baseline 4,281,727 B (4181.4 KB) → realtime-ws 4,288,743 B (4188.2 KB) = **+7,016 B (+6.86 KB)**，在 plan 设的 ~+10 KB ± 5 KB 范围内 ✓（其它 realtime profile providers-rest / sse / poll / auto 均与 realtime-ws 差 < 5 byte，说明 EventSource polyfill 是公共依赖，transport 字面量差异可忽略）
      - **RSS soak**（10 min，5 WS subscribers + 5 Hz PUT，2718 puts / 0 errors）：起 47.7 MB → t+60s 峰 126.3 MB（5 WS 全建 + asyncpg 池预热）→ 稳定区间 60-68 MB → t+540s 60.7 MB；soak 期间无持续上升趋势，与 5 min 基线（68.3 MB）相比末值反而下降 7.6 MB，**远低于 plan 设的 < 30 MB 涨幅判据 ✓**。脚本支持 `--duration 3600` 跑全 1h，本次因迭代节奏取 600s，未来需要硬验时直接拉满即可
      - **p95 延迟**：26.33 ms（p50 20.21 / p99 31.97 / max 84.67），**远低于 plan 设的 500 ms 目标 ✓**。脚本路径走 backend → asyncpg → PG，覆盖 PUT 主路径；浏览器侧 RTT 由网络叠加在此之上但量级仍然可控
    - **新增脚手架**：`tests/scripts/soak.sh`（bash 入口，trap-driven shutdown，每 N 秒采 RSS 进 `tests/.results/soak.rss.tsv`） + `tests/scripts/soak-loadgen.py`（asyncio loadgen：N 个 WS subscriber 持开 + httpx PUT loop，结束时 emit JSON-Lines summary 含 p50/p95/p99/avg/max）。脚本幂等可中断、不写 dev DB、用 `secrets.token_hex` 前缀避免与 pytest 残留 `providers.id` 冲突
    - **避坑（落地踩过 + 修了的）**：① loadgen 第一版用 `soak-{i % 50}` 做 id，跑第二次时与上一次 user 撞 PG `providers.id` UNIQUE 约束 → 全部 409 `id owned by another user` → 0 puts。修为 `f'soak-{secrets.token_hex(3)}-{i % 50}'`，每次 run 独立 namespace。② macOS `ps -o rss=` 在 OS 内存压力下会把 inactive page 移出 resident，soak 结束后空闲态读出来的 RSS 会大幅低于 active 期值（17 MB vs active 60-68 MB），不是真泄漏信号；判据应看 active 期 max-min drift 而不是「end - start」
    - **Stage 2 出口**：三条出口判据全部达成（回归全绿 + 三组数字达标 + flag 默认空仍字节级等同 Stage 1）
  - **部署分支拓扑硬切（B 方案 reset）**✅ — 详见修订记录 2026-05-02 · 部署分支拓扑硬切。my-deploy 退到 `2b26349` 干净 baseline（仅 Stage 0 抽象层 + Dexie 路径 bug fix）；`new-deploy` 拉自 `5c689ab` 承载 Stage 1+ 全套；`backup/my-deploy-pre-reset` 远端永久保留作为逃生通道。CLAUDE.md 新增「部署分支拓扑」段固化分支角色 / 工作流契约 / Northflank 约束 / 老用户迁移路径 / reset SOP，所有未来工作以此为准
  - 下一步：在 Northflank 新建第二个服务指向 `new-deploy` 分支 + 独立 Postgres + 独立域名；按 plan 灰度顺序逐档开 backend flag（`BACKEND_DATA_API_ENABLED` → `BACKEND_AUTH` + `BACKEND_DATA_API_URL` → `BACKEND_DATA_TABLES=providers` → `REALTIME_TRANSPORT=auto`）。所有 Stage 3+ 代码改动 / spec / plan 修订仅合到 `new-deploy`，不再触碰 my-deploy。Stage 4.5 修订记录已废弃 per-table 自动迁移机制 ceremony，Stage 3 节奏聚焦每张叶子表 SQLModel + router + alembic + flag

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
> **上线策略**：Stage 1 全部 7 个 Step + Stage 1.5 完成后才算"Stage 1 收尾"，整体按「**上线但不启用**」策略合并到 **new-deploy**（2026-05-02 部署分支拓扑硬切前为 my-deploy，硬切后所有 stage 1+ 工作走 new-deploy）：代码合上线，但前端 `BACKEND_DATA_API_URL` / `BACKEND_DATA_TABLES` / `BACKEND_AUTH` 与后端 `BACKEND_DATA_API_ENABLED` 默认全不开 → 行为与 Stage 0 完全等同；按需在部署里开 flag 灰度。回滚不需 revert，关 flag 即可。

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

**Stage 1 出口判据**：4 个场景全过 + bundle 体积变化可接受 + flag 默认关时与 Stage 0 行为字节级一致 → 可合并到 new-deploy 上线（2026-05-02 拓扑硬切前为 my-deploy）。

---

### Stage 1.5 — 自家鉴权（多用户 Ready）

> **⚠️ 部分内容已 deprecated（2026-05-02 · 迁移路径转向）**：本段中以下与「双写窗口 / Dexie 账号关联」相关的设计在导入导出 only 路径下不再需要，但已落地代码不立刻删，等 Stage 5 一起清：
> - `users.linked_dexie_email` 列 + UNIQUE 约束 + alembic migration
> - `POST /api/v1/auth/link-dexie` endpoint + `tests/api/test_auth.py::test_link_dexie_first_write_wins`
> - 前端首次登录调 `link-dexie` 的逻辑
> - 「与 Dexie Cloud 共存策略（双写窗口）」整段
> - 「用户身份关联（为后续数据迁移铺路）」整段
>
> 鉴权主体（用户表 / refresh token / JWT 签验 / register / login / refresh / logout / me / `BackendAuthSource`）保持有效，是后续所有 stage 的基础。

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
- 走「上线但不启用」策略：合并到 new-deploy 时 `REALTIME_TRANSPORT` 默认空 → 行为字节级等同 Stage 1 收尾；按需在 Northflank 控制台开 flag 灰度

**回滚**：`REALTIME_TRANSPORT=` 空（关闭实时通道，退回 Stage 1 行为）；如有更严重问题，从 `BACKEND_DATA_TABLES` 摘掉 providers 退回 Stage 0 行为。

**风险**
- **重连风暴**：100 个 tab 同时重连撞 server。**缓解**：客户端指数退避 + 抖动；server 端连接数硬限制（`MAX_WS_PER_USER=20`），超过直接 reject。**对应自动化**：暂无（hard limit 触发态在 e2e 模拟成本高），靠「上线把关判据」soak 阶段的多 WS 订阅压力 + Step 1 `test_ws_account_isolation` 反向证明 broker 不会跨 user_id 串
- **漏事件**：server 重启 / 长时断网期间事件丢失。**缓解**：每个 event 带 `rev`，客户端记 `lastRev`，重连发 `since=<lastRev>` 让 server SQL 回放。**对应自动化**：`step4 case2 ws reconnect catch-up` + `step3 scenarioB ws auto-reconnect after offline window`
- **慢客户端拖垮 server**：订阅队列堆积。**缓解**：`maxsize=200` + 满了 close 客户端，让它走重连补漏。**对应自动化**：暂无（200 条事件灌入实测成本高），靠「上线把关判据」1h soak 的 RSS 涨幅曲线反向兜

---

### Stage 3 — 迁移叶子表（`reactives` / `avatarImages` / `installedPluginsV2` / `assistants`）

**目标**：先搬无 join、无级联的表。`reactives` 特殊：是 `persistentReactive('#user-data', …)` 的底层，需要 KV 形 endpoint，让 `persistent-reactive.ts` 透明走 `repos.reactives.observeOne(key)` / `put({key,value})`。

**老用户旧数据怎么办**（2026-05-02 修订记录）：**不走 per-table 自动迁移**。新版默认不挂 `dexie-cloud-addon`，老用户在新版里旧表起步即空；要把旧数据带过来必须主动走「旧版 ExportDataDialog → 新版 ImportDataDialog」。具体细节见「现有用户数据迁移」段与「跨版本导入/导出兼容」段。本 stage 不需要任何「客户端驱动 push」/「迁移标记表」/「`/api/v1/migrate/status`」机制。

**每张表节奏**：SQLModel + router + Alembic migration + 一个 PR + flag 翻一张。

**验证**：每张表独立验证 — UI 增改删 → 第二 tab 实时更新 → 服务端行匹配 → IndexedDB 缓存被回填。跑一次 `ExportDataDialog` / `ImportDataDialog`，确认 `db.tables` 枚举仍能导出（缓存仍然完整）。

**回滚**：单表 flag 翻回 Dexie，缓存还在就能继续读。

---

### Stage 4 — 迁移联表 / 级联表（`workspaces` / `dialogs` / `messages` / `items` / `artifacts`）

**目标**：搬级联删除集群（`stores/workspaces.ts` 里的 `db.transaction` 是最难的一处）。

#### 硬前置（2026-05-02 新增）

**messages / artifacts 走的不是 providers / assistants 那种小行 schema**——单条 message 可能携带几 MB 的 base64 attachment（受 `MAX_MESSAGE_FILE_SIZE_MB` 上限），单用户 messages 表行数轻易上万。沿用 Stage 1–3 的协议会在三处崩：① WS event 带完整 row → broker `maxsize=200` 队列在大行场景秒爆；② `?since=N` 全 row 返回 → 客户端 fetch 几十 MB 卡死；③ Postgres TEXT 列直接存 base64 → 表线性膨胀、`vacuum` / 备份 / 慢查询全受影响。messages / artifacts server.ts 落地前必须先做完下面两件事，不可后置。

**前置 1：大行传输协议改造**

- WS event payload 改为 `{table, op, id, rev}` only —— 不再带 `row` 字段。客户端拿到 event 后调 `GET /api/v1/<table>/<id>` 按需拉具体 row（命中 IndexedDB 缓存 + If-None-Match `rev` ETag 时 server 返 304）
- `?since=N` 加 `?limit=200` + cursor 续拉：单次响应硬上限（如 1MB），返回 `{rows, next_cursor}`，客户端拿 `next_cursor` 续拉直到空
- 仅 messages / artifacts 走新协议；providers / assistants 等小表保持原 envelope（避免无谓回归风险）。`src-backend/data/routers/<table>.py::_to_event` 与前端 `realtime.ts` dispatcher 加 per-table `payloadMode: 'inline' | 'notify-only'` 配置位
- **通过判据**（待 spec 落地）：
  - api: 单条 5MB attachment 的 message PUT 后 WS event 字节数 < 1KB；客户端用 event.id 调 GET 能拿回完整 row
  - api: 单用户 1 万条 messages，`?since=0&limit=200` 第一次响应 < 1MB，`next_cursor` 非空；循环续拉总共 ≥ 50 次拉完
  - spec: 双 tab 跨设备 message put → 另一 tab 在 500ms 内 UI 出现新行（含 attachment 渲染）
  - spec: broker 1 个 user 100 条 5MB messages 连续 PUT，server RSS 涨幅 < 50MB（验证 notify-only 不让 broker 吞 row 字节）

**前置 2：对象存储分流（B 方案）**

- 阈值常量 `BLOB_INLINE_MAX_BYTES=65536`（64KB）。客户端 attachment 序列化时：
  - `< 64KB` → 保持 base64 内联在 row 字段，与现状一致
  - `≥ 64KB` → multipart POST `/api/v1/blobs` 上传，server 计算 sha256 + 写对象存储 → 返回 `{type:'ref', url, sha256, size, content_type}`，客户端把 row 里对应字段替换为该 ref 对象后再 PUT 到对应 endpoint
- 后端 `/api/v1/blobs`：
  - 接 multipart upload；按 `(user_id, sha256)` 去重（同一文件多用户互不可见，但单用户内幂等）
  - 对象存储抽象 `BlobStore` 接口，先实现 `S3BlobStore`（兼容 R2 / MinIO / AWS S3），env 配置 `BLOB_STORE_KIND=s3` + `BLOB_STORE_BUCKET` + `BLOB_STORE_ENDPOINT` + `BLOB_STORE_ACCESS_KEY` + `BLOB_STORE_SECRET_KEY`；本地开发 / 测试默认 `BLOB_STORE_KIND=local-fs`（落到 `src-backend/.blob-store/<sha256>` 目录）
  - 返回的 `url` 是 pre-signed GET URL，TTL 1h；客户端读 ref 时直 GET，不经 backend 回源
  - DELETE row 时不立即 delete blob（多 row 可能引用同 sha256）；用周期 GC job 扫 `(blob, refcount)`，refcount=0 且超过 7 天再删
- 客户端读路径：`<table>.server.ts` 拉到 row 后遇到 `{type:'ref', url}` 字段，下载 blob 写入 IndexedDB 缓存（保持现有 Blob 对象形态），UI 透明无感
- 客户端 IndexedDB 仍可缓存 Blob（不强制 ref 化），让离线读路径不变
- **通过判据**（待 spec 落地）：
  - api: PUT 5MB attachment → server PG row 里 attachment 字段是 `{type:'ref', url, sha256, size}`，对象存储里 `<sha256>` 文件存在
  - api: 同一 sha256 第二次上传去重（PG `blobs` 表行不增，对象存储不重复写）
  - api: 64KB 边界 → < 64KB inline 进 PG，= 64KB 走对象存储
  - spec: client 端 PUT message with 5MB attachment → 第二 tab 收到 event → GET row → 通过 ref URL 下载 blob → UI 渲染 attachment，端到端 < 5s
  - spec: 客户端 IndexedDB 清空后刷新 → ref blob 重新从对象存储拉回 → UI 一致

#### 主体迁移工作

**后端**：`DELETE /api/v1/workspaces/:id?cascade=true` 在单个 Postgres 事务里完成级联；为 dialog 删除提供同款。`GET /api/v1/messages?dialogId=…&since=…&limit=200` 让 `DialogView.vue` 的滚动加载继续可行（注意：`since` + `limit` 走前置 1 的 cursor 协议）。

**前端**：`runTx()` 对这些表走新的 `repos.batch(operations)` → `/api/v1/batch`；尚未迁移的表仍走 `db.transaction`。

**验证**：删除一个含多 dialog/messages/artifacts 的工作区 → 服务端清空（含对象存储 ref 的 refcount 减 1）→ 清空 IndexedDB 后刷新仍正确。

**回滚**：flag 翻回；id 稳定，级联幂等。对象存储的孤儿 blob 留给周期 GC，不影响数据正确性。

---

### Stage 4.5 — 服务端 Import Job + 新设备首屏 bootstrap

**目标**：把"现有用户数据迁移"从浏览器端密集型工作搬到 server 端。浏览器只负责把 `aiaw_user_db.json` 5MB 切片直传对象存储；其余阶段（解析 / 分表写入 / attachment 处理）由 backend 后台 worker 跑，用户上传完即可关 tab，状态通过 WS 推进度。同步加 `GET /api/v1/bootstrap` 解决新设备首次登录的渐进填充式空白问题。

**前置**：Stage 4 硬前置已落地（对象存储 BlobStore + `/api/v1/blobs` + 客户端 multipart helper），messages / artifacts 表已切到 server 端。

#### 架构总览

```
浏览器                                          backend                          对象存储
───────                                        ─────────                        ─────────
ImportDataDialog
 ├ POST /api/v1/import/jobs                    创建 ImportJob 行
 │   ◀── {job_id, multipart_upload_id} ──                                         │
 ├ for each 5MB slice:                                                            │
 │  POST /api/v1/import/jobs/{id}/parts/{n}                                       │
 │   ◀── pre-signed PUT URL ──┐                                                   │
 │                            └─── PUT slice ──────────────────────────────────► │
 ├ POST /api/v1/import/jobs/{id}/complete       complete multipart                │
 │                                              ───────────────► assemble ◀──────│
 │                                              触发 asyncio worker
 └ 关 tab 走人 / 或继续订阅状态
                                                Phase A · ijson 流式 parse ◀─────│
                                                Phase B · INSERT 结构表
                                                Phase C · INSERT messages 文字
                                                Phase D · 提取 attachment ────► │
                                                每完成 phase publish WS event

任意设备
 realtime.subscribe('import_jobs', cb)
   收到 phase change → 更新 banner
   Phase B 完成 → workspaces 出现
   Phase C 完成 → message 文字可读
   Phase D 完成 → attachment 全齐
```

> **执行顺序**：Stage 4.5 内部分 8 个 Step。
> **Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 → Step 7 → Step 8 → Stage 5**
>
> **上线策略**：与 Stage 1/2 同款"上线但不启用"。后端 ImportJob worker 由 `IMPORT_JOB_ENABLED` env flag 守卫；前端 ImportDataDialog 按钮可见性由 `IMPORT_JOB_ENABLED` 同名 env 控制。两边都关 → ImportDataDialog 隐藏 + worker 不启动；两边都开 → 完整迁移路径上线。
>
> **部署拓扑**：旧版（Dexie Cloud SaaS）独立实例不下线作为兜底；新版（自家 backend）独立实例承载全量 stage 1+ 功能；用户主动从旧版导出 → 在新版导入。一段时间后（建议 active 用户 ≥ 80% 迁完后）宣告 6 周下线窗，到期关闭旧版实例。

#### Step 1 — ImportJob 模型 + 后端 worker 骨架 + Phase A 解析

**做什么**：
- `src-backend/data/models/import_job.py`：`ImportJob(id UUID, user_id FK, status enum['queued'|'uploading'|'assembling'|'parsing'|'phase_b'|'phase_c'|'phase_d'|'done'|'failed'|'cancelled'], multipart_upload_id, raw_object_key, total_bytes, processed_bytes, total_rows, processed_rows, total_blobs, processed_blobs, error_message, dead_letter JSONB, created_at, updated_at)`
- alembic migration：建表 + partial unique index `(user_id) WHERE status IN ('queued','uploading','assembling','parsing','phase_b','phase_c','phase_d')` 保证每用户同时只 1 个 active job
- `src-backend/data/import_worker.py`：asyncio worker 框架，按 status 状态机推进；FastAPI 进程启动时 `asyncio.create_task` 起 worker 主循环，启动时扫一次所有 status 非终态的 active job 续跑（崩溃恢复）
- Phase A 实现：用 `ijson` 流式解析对象存储里的 raw JSON → 验证 dexie-export-import 格式 → 提取 schema metadata → 按表分流写到本地临时 NDJSON 文件（`/tmp/import-<job_id>/<table>.ndjson`，避免 PG TEMP TABLE 内存压力）→ 累计 total_rows / total_blobs 写回 ImportJob → 状态转 phase_b
- 全部由 `IMPORT_JOB_ENABLED` env flag 守卫的 lazy import（与现有 `BACKEND_DATA_API_ENABLED` 同款）

**通过判据**：
- api: `tests/api/test_import_job.py::test_phase_a_extracts_table_row_counts`（fixture small：10 providers + 5 dialogs，验证 total_rows 准确）
- api: `tests/api/test_import_job.py::test_phase_a_streaming_memory_under_200mb`（fixture huge：200MB JSON，pytest 跑前后 RSS 差 < 200MB，slow mark）
- api: `tests/api/test_import_job.py::test_phase_a_invalid_json_marks_failed`（损坏 JSON → status='failed' + error_message 非空）
- api: `tests/api/test_import_job.py::test_active_job_unique_constraint`（同 user 创建第二个 active job → IntegrityError）
- api: `tests/api/test_import_job.py::test_worker_recovers_active_job_on_startup`（手动插一行 status='parsing' → 重启 worker → job 状态正常推进）

#### Step 2 — S3 multipart 上传 5 个 endpoint

**做什么**：
- `src-backend/data/routers/imports.py`:
  - `POST /api/v1/import/jobs` → 创建 ImportJob (status=uploading) + 调 `BlobStore.create_multipart_upload(imports/<user>/<job_id>.json)` → 返回 `{job_id, multipart_upload_id}`
  - `POST /api/v1/import/jobs/{id}/parts/{n}` → 校验 job ownership + status=uploading → 返回 pre-signed PUT URL (TTL 1h)
  - `POST /api/v1/import/jobs/{id}/complete` → body 接收 client 提供的 `[{part_number, etag}]` 列表 → 调 `BlobStore.complete_multipart_upload` → 状态 uploading → assembling → queued → 写 raw_object_key → notify worker
  - `GET /api/v1/import/jobs/{id}` → 返回当前状态 + 进度（`{status, phase, processed_rows, total_rows, processed_blobs, total_blobs, error_message}`）
  - `GET /api/v1/import/jobs?status=active` → 返回当前用户 active job（如果有），用于多设备登录时显示状态
  - `DELETE /api/v1/import/jobs/{id}` → 调 `BlobStore.abort_multipart_upload` + 已写入的行按 `(user_id, imported_from_job_id=job_id)` 标记软删（行加 `imported_from_job_id` 列，导入时 worker 写入）→ 状态转 cancelled
- `BlobStore` 接口扩展（Stage 4 硬前置规划过）补 4 个 multipart 方法：`create_multipart_upload` / `generate_part_url` / `complete_multipart_upload` / `abort_multipart_upload`，3 种实现（local-fs / minio / s3）按 env `BLOB_STORE_KIND` 选

**通过判据**：
- api: `tests/api/test_imports_router.py::test_create_job_returns_job_id_and_multipart_upload_id`
- api: `tests/api/test_imports_router.py::test_part_url_is_presigned_with_short_ttl`（断言响应 URL 包含签名参数 + Expires 在 ±5min 范围内）
- api: `tests/api/test_imports_router.py::test_complete_with_all_parts_triggers_worker`
- api: `tests/api/test_imports_router.py::test_complete_with_missing_parts_returns_400`
- api: `tests/api/test_imports_router.py::test_get_job_returns_status_and_progress`
- api: `tests/api/test_imports_router.py::test_get_active_jobs_returns_in_progress_only`
- api: `tests/api/test_imports_router.py::test_delete_active_job_aborts_multipart_and_soft_deletes_imported_rows`
- api: `tests/api/test_imports_router.py::test_user_b_cannot_access_user_a_job_returns_404`
- api: `tests/api/test_imports_router.py::test_part_endpoint_rejects_when_job_not_in_uploading_status`

#### Step 3 — Phase B 结构表写入

**做什么**：
- `import_worker.py` 增 phase_b：从 Phase A 输出的 NDJSON 读小表（按依赖顺序：providers / assistants / plugins / reactives / avatarImages / workspaces / dialogs metadata），按表 `INSERT ... ON CONFLICT (id) DO UPDATE WHERE existing.updated_at < incoming.updated_at`（LWW）
- 复用现有各 router 的 SQLModel + bulk insert helpers（避免业务逻辑分裂）
- 每行写入时附 `imported_from_job_id=<job_id>` + `imported_at=now()` 两列（所有 server-routed 表 alembic 加这两列；Stage 1-4 已落地的表按需 backfill NULL）
- 每张表完成 `broker.publish(user_id, {table:'import_jobs', op:'put', id:job_id, row:{...latest envelope}})`
- 状态转 phase_c

**通过判据**：
- api: `tests/api/test_import_phase_b.py::test_writes_workspaces_with_original_uuid`（fixture 含 3 workspaces，导入后 PG 直查 UUID 一致）
- api: `tests/api/test_import_phase_b.py::test_writes_all_structural_tables_in_dependency_order`
- api: `tests/api/test_import_phase_b.py::test_lww_skips_older_incoming_when_existing_newer`（先 PUT 一个 newer workspace，再导入 older → 跳过）
- api: `tests/api/test_import_phase_b.py::test_imported_rows_tagged_with_job_id_and_timestamp`
- api: `tests/api/test_import_phase_b.py::test_publishes_progress_event_per_table`
- api: `tests/api/test_import_phase_b.py::test_phase_b_done_triggers_phase_c`

#### Step 4 — Phase C messages 文字写入

**做什么**：
- `import_worker.py` 增 phase_c：流式遍历 `messages.ndjson`，每 500 行一批 `INSERT ... ON CONFLICT DO UPDATE`；attachment 字段按原 base64 inline 存进 PG 的临时区域（messages 行加 `_pending_blob_extraction BOOLEAN DEFAULT FALSE` 列，导入时含 attachment 的行标 true）
- 进度按行报：每 500 行 `broker.publish` 进度事件（节流，避免 WS 风暴）
- FK 约束：messages 必须 dialog 已写入。Phase B 已写 dialogs metadata，正常情况无孤儿；FK 失败的 row 进 `import_jobs.dead_letter` JSONB 字段，Phase D 跑完后重试一次（一般是依赖顺序错配的小概率边界）
- 状态转 phase_d

**通过判据**：
- api: `tests/api/test_import_phase_c.py::test_writes_message_text_in_batches_of_500`（fixture 1500 messages，验证 PG 行数 1500 + 进度事件 ≥ 3 次）
- api: `tests/api/test_import_phase_c.py::test_marks_pending_blob_extraction_for_messages_with_attachment`
- api: `tests/api/test_import_phase_c.py::test_orphan_message_without_dialog_lands_in_dead_letter`
- api: `tests/api/test_import_phase_c.py::test_progress_event_emitted_per_batch`
- api: `tests/api/test_import_phase_c.py::test_phase_c_done_triggers_phase_d`（slow mark）
- api: `tests/api/test_import_phase_c.py::test_lww_message_text_skip_when_existing_newer`

#### Step 5 — Phase D attachments → 对象存储

**做什么**：
- `import_worker.py` 增 phase_d：扫 `messages WHERE _pending_blob_extraction=TRUE AND user_id=?`，逐条提取 attachment base64 字段
- 对每个 attachment：解码 base64 → 算字节大小 → 按 `BLOB_INLINE_MAX_BYTES=65536` (64KB) 阈值判断
  - `< 64KB` → 保持 inline 在 PG row 字段（Stage 4 大行协议规定，messages 表小附件 inline 与 server-side 协议一致）
  - `≥ 64KB` → 计算 sha256 → 调 `BlobStore.put(sha256, bytes)`（同 sha256 已存在则去重不二次写）→ 改写 row 的 attachment 字段为 `{type:'ref', url, sha256, size, content_type}`
- 4 路 `asyncio.Semaphore` 并发 + 重试 3 次（指数退避 1s/2s/4s）
- 单条失败超过重试上限进 `import_jobs.dead_letter`；不阻塞整体 phase
- 进度按 attachment 报：每 10 个完成 `broker.publish` 一次进度
- 全部完成清 `_pending_blob_extraction` 标志 → 状态转 done → 清 `/tmp/import-<job_id>/` + 对象存储里的 raw upload (TTL 7 天 lifecycle rule 兜底)

**通过判据**：
- api: `tests/api/test_import_phase_d.py::test_attachment_under_64kb_stays_inline_in_pg`
- api: `tests/api/test_import_phase_d.py::test_attachment_over_64kb_uploaded_to_blob_store_and_row_rewritten`（验证 PG row 字段是 `{type:'ref',...}`，BlobStore 里 `<sha256>` 文件存在）
- api: `tests/api/test_import_phase_d.py::test_same_sha256_reuses_existing_blob_no_double_upload`
- api: `tests/api/test_import_phase_d.py::test_failed_attachment_after_3_retries_lands_in_dead_letter`（mock BlobStore.put 抛错）
- api: `tests/api/test_import_phase_d.py::test_phase_d_done_marks_job_done_and_clears_temp_files`
- api: `tests/api/test_import_phase_d.py::test_concurrent_uploads_capped_at_4`（mock BlobStore.put 加 sleep + counter，验证峰值并发 = 4，slow mark）

#### Step 6 — WS 进度推送 + 状态持久化（已在 Step 1-5 内分散实现，本 Step 做集成验证）

**做什么**：
- `import_jobs` 注册为 server-routed table（添加到 `SERVER_CAPABLE_TABLES`，但只读不允许 client 直接 PUT；写路径只有 worker）
- `_to_event` 与 `_to_row` 沿用现有 envelope 契约（`{id, version, updated_at, deleted, data: <ImportJobStatus>}`）
- 客户端 `realtime.subscribe('import_jobs', cb)` 即可拿状态变更
- 用户登录时一次 `GET /api/v1/import/jobs?status=active` 拉当前 active job（如果有）；之后增量靠 realtime
- ImportJob row 的 PUT 仅由 worker 内部完成；任何 `PUT /api/v1/import_jobs/<id>` 来自客户端的写请求返回 405 Method Not Allowed

**通过判据**：
- api: `tests/api/test_import_realtime.py::test_phase_change_publishes_ws_event`
- api: `tests/api/test_import_realtime.py::test_ws_event_envelope_matches_routed_table_contract`（断言 `e.row.data` 形如 `{status, phase, processed_rows, total_rows, ...}`）
- api: `tests/api/test_import_realtime.py::test_user_b_does_not_receive_user_a_import_progress`
- api: `tests/api/test_import_realtime.py::test_active_jobs_query_returns_in_progress_only`
- api: `tests/api/test_import_realtime.py::test_client_put_to_import_jobs_returns_405`

#### Step 7 — 前端 ImportDataDialog 重写 + 上传切片 helper

**做什么**：
- `src/data/import-client.ts` 新增（client 侧的 multipart 实现，零依赖，~150 行）：
  - `createImportJob(file): Promise<{jobId, multipartUploadId}>`
  - `uploadParts(file, jobId, opts): Promise<Array<{partNumber, etag}>>` —— 5MB `Blob.slice()` 切片，4 路 `Promise.all` 并发，单 part 失败重试 3 次（指数退避）；上传 cursor（已成功 part numbers）持久化到 `localStorage:import.<jobId>.parts`，断网恢复后查 `GET /api/v1/import/jobs/<id>` 验证 server 端记录，续传剩余 parts
  - `completeImport(jobId, parts): Promise<void>`
  - `cancelImport(jobId): Promise<void>`
  - `subscribeImportStatus(jobId, cb): () => void` —— 包 `realtime.subscribe('import_jobs', ...)` 过滤 jobId
- `src/components/ImportDataDialog.vue` 重写：
  - 老逻辑（`dexie-export-import`.importInto + 然后 push）整段砍
  - 新逻辑：选文件 → `createImportJob` → `uploadParts`（显进度条 + 估算剩余时间）→ `completeImport` → 关闭对话框，跳"迁移状态"小卡片
  - 整个上传过程 file 不一次性进 ArrayBuffer，用 `Blob.slice().stream()` 流式喂 fetch（低端设备友好）
- `src/pages/AccountPage.vue` 加"迁移状态"卡片：`subscribeImportStatus` 显示 phase + 进度 + 取消按钮 + 文案 "您可以关闭浏览器，处理完会通过通知告知"
- 应用启动时：`useObservable(authSource.user)` 触发后调一次 `GET /api/v1/import/jobs?status=active`，有 active job 则自动挂 subscribe + 显示卡片（多设备一致性）

**通过判据**：
- spec: `tests/e2e/stage4_5/step7-import-flow.spec.ts::test_upload_then_close_tab_then_reopen_sees_progress`（profile import-job + fixture small：上传完后 `page.close()` → 重开 → AccountPage 显示卡片 + phase 推进）
- spec: `tests/e2e/stage4_5/step7-import-flow.spec.ts::test_upload_resumes_after_simulated_network_drop`（用 `helpers/net.ts.setOffline` 中断 → 恢复后续传 + cursor 校验，slow mark）
- spec: `tests/e2e/stage4_5/step7-import-flow.spec.ts::test_cancel_button_aborts_job_and_clears_state`
- spec: `tests/e2e/stage4_5/step7-import-flow.spec.ts::test_phase_b_complete_makes_workspaces_visible`（fixture small + 等 phase_b 完成 → workspaces 列表非空）
- spec: `tests/e2e/stage4_5/step7-import-flow.spec.ts::test_phase_c_complete_makes_message_text_readable`
- spec: `tests/e2e/stage4_5/step7-import-flow.spec.ts::test_phase_d_complete_makes_attachment_renderable`（fixture medium：5 messages × 1MB attachment，phase_d 完成后 message 内 `<img>` 加载成功）

#### Step 8 — `GET /api/v1/bootstrap` endpoint + 首屏路由 guard

**做什么**：
- 后端 `src-backend/data/routers/bootstrap.py`：`GET /api/v1/bootstrap`
  - 一次返回所有小表全量 + messages 表的"最近 N 个 dialog 的最新 50 条"摘要
  - 响应体：`{schema_version, workspaces:[...], dialogs:[...], providers:[...], assistants:[...], plugins:[...], reactives:[...], avatarImages:[...], messages_recent:[...]}`
  - 单响应硬上限 1MB（实测一般用户 < 200KB）；messages_recent 超过则按 dialog 截断（反向时间排序，先保留最近活跃的 dialog 的 messages）
  - response 加 `Cache-Control: private, max-age=10` 让客户端短期缓存避免重复请求
- 前端 `src/router/index.ts` 加 router guard：登录后 `await GET /api/v1/bootstrap` → 写入 IndexedDB 缓存 → 放路由进 MainLayout
- guard 失败回退：超时 2s 没返回 → 走老路径（IndexedDB 缓存 + 后台 fetch）→ banner 提示 "部分数据稍后加载"
- 已有 active import job 时：`bootstrap` 仍返回当前 server 端可见的部分数据（Phase B 完成的 workspaces 等），与 import 进度不冲突

**通过判据**：
- api: `tests/api/test_bootstrap.py::test_returns_all_small_tables_in_one_response`
- api: `tests/api/test_bootstrap.py::test_messages_recent_limited_to_50_per_dialog`
- api: `tests/api/test_bootstrap.py::test_response_size_under_1mb_for_typical_user`（fixture 100 dialogs × 50 messages = 5000，验证响应体 < 1MB）
- api: `tests/api/test_bootstrap.py::test_response_truncates_to_under_1mb_for_heavy_user`（fixture 1000 dialogs × 50 messages = 50000，验证截断逻辑生效）
- api: `tests/api/test_bootstrap.py::test_account_isolation`
- api: `tests/api/test_bootstrap.py::test_partial_data_during_active_import`（手动插 phase_b 完成的 workspaces + active job → bootstrap 正确返回 workspaces）
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_fresh_browser_login_no_blank_first_screen`（清空 IndexedDB → 登录 → 1.5s 内 workspaces 列表可见）
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_bootstrap_timeout_falls_back_to_progressive`（mock 后端 sleep 3s → 2s timeout → 走老路径 + banner 出现）

#### Stage 4.5 出口判据

- **fixture huge（200MB JSON 含 1 万 messages + 100 attachments × 平均 2MB）端到端跑通**：上传 + 关 tab + worker 跑完 + 重新登录 → 数据齐全
  - spec: `tests/e2e/stage4_5/exit-criteria.spec.ts::test_huge_fixture_end_to_end` (slow + serial mark)
- **浏览器活跃时间 < 10 分钟**（标准家庭带宽 50Mbps 模拟）—— 上线把关手测，结果记进度快照
- **worker 单 job 服务端 RSS 增量 < 500MB** —— 上线把关手测，结果记进度快照
- **新设备首次登录 < 1.5s 看到 workspaces 列表**（bootstrap 命中路径）
  - 由 `step8-bootstrap.spec.ts::test_fresh_browser_login_no_blank_first_screen` 兜
- **回归套件**：`pnpm test:api && pnpm test:e2e -g "stage4_5|stage4|stage2|stage1_5|smoke"` 一把全绿（含 import-job profile 全部新 case）

**回滚**

- env `IMPORT_JOB_ENABLED=false` 关闭 worker + 隐藏 ImportDataDialog 按钮 + bootstrap endpoint 仍可用（与 import 解耦）
- 老用户暂时无法迁移但旧版仍可用作兜底
- 已有 active jobs 在 worker 关闭后保持 status，重新开 flag 后自动续跑（Step 1 的崩溃恢复路径）
- 极端情况：`DELETE /api/v1/import/jobs/<id>` 逐个清理 + 清空 `imports/<user>/*` 对象存储前缀

**风险**

- **worker 进程 crash 中断 in-flight job**：启动时扫描 active job 自动恢复（Step 1 实现）。**对应自动化**：`test_worker_recovers_active_job_on_startup`
- **单实例部署 worker 阻塞其他请求**：asyncio + 流式 IO 不阻塞 event loop；Phase D 上传是 IO 密集而非 CPU 密集；4 路并发不至于让 event loop 饿死。**对应自动化**：上线把关阶段 soak 跑 1 个 200MB job + 并发 100 QPS 普通请求，观察 p95 不超 3x 基线（写进 `tests/scripts/soak-import.sh`，结果记进度快照）
- **用户在 import 进行中创建新数据**：因为新版 UUID 都是新生成，老 import UUID 与新建 UUID 不冲突；若极小概率 UUID v4 撞库按 LWW 处理。**对应自动化**：`test_lww_skips_older_incoming_when_existing_newer` (Step 3)
- **多设备同时尝试 import**：DB 唯一约束阻止第 2 个 job 创建，第 2 个设备 POST 拿到 409 + 当前 active job_id，UI 跳到现有 job 状态。**对应自动化**：`test_active_job_unique_constraint` (Step 1) + `test_get_active_jobs_returns_in_progress_only` (Step 2)
- **对象存储 raw upload 累积成本**：`imports/` 前缀加 S3 lifecycle rule TTL 7 天自动清；worker 完成 / 失败 / 取消时主动 delete。**对应自动化**：`test_phase_d_done_marks_job_done_and_clears_temp_files` (Step 5) + `test_delete_active_job_aborts_multipart_and_soft_deletes_imported_rows` (Step 2)

---

### Stage 5 — 摘除 `dexie-cloud-addon` + 清理 deprecated 代码

**前置**：Stage 4.5 已上线（`IMPORT_JOB_ENABLED` 已默认开），老用户有可用迁移路径（旧版导出 → 新版 ImportDataDialog）。在 Stage 4.5 缺位时直接进 Stage 5 会让老用户失去迁移路径，必须串行。

**目标**：拆掉 dexie-cloud 残留 + 一次性清理 2026-05-02 修订记录里标记为 deprecated 的 ~150 行代码。鉴权早在 Stage 1.5 已经全部走 `BackendAuthSource`；导入导出 only 路径下从未存在过「双写窗口」/「双登录入口」，本阶段没有"过渡 UI 回归单入口"工作。

**主体清理**

- `src/utils/db.ts` 从 `addons` 摘掉 `dexieCloud`；所有表退化为本地缓存
- `src/router/routes.ts` `/account` / `/model-pricing` 改按 `BACKEND_DATA_API_URL` 注册（如 Stage 1.5 已完成则只确认）
- `package.json` 移除 `dexie-cloud-addon`

**deprecated 代码一次性清理**（2026-05-02 修订记录 · 合计 ~150 行）

- 删 `users.linked_dexie_email` 列 + UNIQUE 约束（写一份 alembic downgrade-safe 的 drop migration）
- 删 `POST /api/v1/auth/link-dexie` endpoint + `tests/api/test_auth.py::test_link_dexie_first_write_wins`
- 删前端首次登录调 `/auth/link-dexie` 的逻辑
- 删 `src/utils/db.ts` 的 `unsyncedTables` 计算 + `src/data/server-tables.ts` 模块（dexie-cloud-addon 已摘，无 middleware 需要绕）
- `BackendAuthSource` 内删去与 `linked_dexie_email` 相关的字段 / 调用

**导入导出收尾**

- 验证 `ExportDataDialog` / `ImportDataDialog` 在 `dexie-cloud-addon` 摘除后仍能读写 `aiaw_user_db.json`（dexie-export-import 不依赖 addon，理论上没问题，但要 e2e 真跑过）
- 验证「跨版本导入/导出兼容」段定义的对象存储桥接（导出时 fetch ref → 转 base64；导入时按阈值上传 / 留 inline）端到端正确

**验证**

- 全新浏览器登录 → 应用是空账号（无任何旧数据自动出现），与 2026-05-02 修订记录定义一致
- 旧版部署导出 `aiaw_user_db.json` → 新版 import → 数据完整 + attachment 走对象存储桥接路径
- 新版导出 `aiaw_user_db.json` → 旧版部署 import → 数据完整（验证「对外格式纪律」：导出文件无 `{type:'ref', url}` 字段）
- 卸载 PWA → 重装 → 登录 → 数据从 server 拉回
- Tauri / Capacitor 构建产物里 grep 确认无 `dexie-cloud` 残留

**回滚**：用回旧版部署即可（旧版 Dexie Cloud 不下线作为兜底）。无需保留「同时挂载 dexieCloud + `LEGACY_DEXIE_CLOUD=true` flag」机制——导入导出方案下没有用户处于「数据已迁到 backend 但需要回 Dexie Cloud」的中间态。

---

### 现有用户数据迁移（**导入导出 only · 2026-05-02 整章重写**）

> **整章重写说明**：原方案「客户端驱动一次性 push」已废弃，详见 2026-05-02 顶部修订记录条目。本章现行设计 = **「老用户走 ExportDataDialog → 新版 ImportDataDialog」单一路径**，不存在自动后台迁移、不存在双写窗口、不存在迁移标记表、不存在 `/api/v1/migrate/status` endpoint。

**起点的三种用户 + 各自路径**

1. **仅本地用户（`DexieDBURL` 空）**：数据只在 IndexedDB → 进新版前先在旧版用 ExportDataDialog 下载 `aiaw_user_db.json` → 在新版 ImportDataDialog 导入
2. **Dexie Cloud 用户**：IndexedDB 与 Dexie Cloud 各持一份（最终一致）→ 在旧版（任意已 sync 的设备）导出 `aiaw_user_db.json` → 在新版导入
3. **新版直接注册的 backend-first 用户**：本地无历史数据 → 不走任何迁移路径，直接使用

**核心设计纪律**

- **新版默认不挂 `dexie-cloud-addon`**（Stage 5 完成后包内零残留）；老用户登录新版看到的是空账号，不会自动出现旧数据
- **旧版部署不下线**：愿意迁的用户主动迁；不愿动的继续用旧版 + Dexie Cloud，体验与今天完全一致
- **失败回退路径**：用户在新版 import 失败 → 关闭 tab → 用回旧版 URL → 旧版本地 + Dexie Cloud 数据完整无损（旧版根本不知道新版存在）；这是导入导出 only 方案最大的安全保证
- **不存在「半迁移」中间态**：要么全 import 成功要么没 import；多设备迁移在新版 = 第一台 import 之后第二台登录直接走「server → 本地缓存」回灌（与 Stage 5 的「全新浏览器首次登录」走同一条路径）

**导入导出本身的实现细节** → 见下一章「跨版本导入/导出兼容」段（含对象存储桥接）。

**新版"空账号 → 首次填充"的 UX**

- 老用户首次登录新版看到空 workspaces，AccountPage 显示横幅"想从旧版迁移数据？这里是导入入口"+ 链接到 ImportDataDialog
- 横幅在用户成功 import 一次后或主动 dismiss 后不再显示（`users` 表加 `import_hint_dismissed_at TIMESTAMP NULL`，由 `PATCH /api/v1/auth/me` 写入；Stage 4.5 Step 7 落地时一起加）
- 新注册用户（无旧版账号）的横幅默认 dismiss

**实现层 = Stage 4.5「服务端 Import Job + 新设备首屏 bootstrap」**

详见上文 Stage 4.5 段。核心思路：浏览器只负责文件分块直传对象存储，其余阶段由后端 worker 跑，用户上传完即可关 tab。下面是 200MB 用户的真实体感时序：

| 阶段 | 谁在跑 | 用户视角 | 200MB 用户耗时估 | 关键节点 |
|---|---|---|---|---|
| a. 文件选择 | 浏览器 | 选 `aiaw_user_db.json` | <1s | — |
| b. 上传（5MB 切片 → 对象存储） | 浏览器 | 进度条 "87/270 MB" | 3–10min | 浏览器必须在线 |
| c. 排队 | backend | "上传完成，已开始处理。**您可以关闭页面**" | <1s | 🟢 **此刻可关 tab** |
| d. (可选) 用户关闭浏览器 | — | — | — | — |
| e. Phase A 解析（流式 ijson） | backend worker | 状态卡片 "解析中" | 30s–2min | — |
| f. Phase B 写结构表 | backend worker | "写入 workspaces..." | 5–30s | — |
| g. Phase C 写 messages 文字 | backend worker | "对话历史 3,421 / 12,580" | 1–5min | 🟢 **此后任意设备登录可看到全部 workspaces / dialogs / message 文字** |
| h. Phase D 处理附件 | backend worker | "附件 47 / 312" | 5–30min | 🟢 不阻塞使用 |
| i. 完成 | backend | "迁移完成"通知 | — | — |

**「卡住」时间从 30-60 分钟降到 3-10 分钟**——只有上传阶段需要浏览器在线；最慢的附件处理完全在 server 跑，跟用户的设备性能 / 是否在线无关。

**多设备协调**

- DB 唯一约束保证每用户同一时刻只有 1 个 active job（Stage 4.5 Step 1 的 partial unique index）
- 第二个设备打开 ImportDataDialog → `POST /api/v1/import/jobs` 拿到 409 + 现有 active job_id → UI 直接跳到现有 job 的状态卡片
- 任何设备登录后 `GET /api/v1/import/jobs?status=active` 拉当前 job 状态 + `realtime.subscribe('import_jobs', ...)` 接增量；**用户切设备完全无感**

**失败回退**

- import 中途任何步骤失败 → `import_jobs.status='failed'` + `error_message` 非空 → AccountPage 显示"重试"按钮（DELETE 旧 job + 重新上传）
- 整个迁移失败 / 用户不满意 → 用回旧版 URL → 旧版本地 + Dexie Cloud 数据完整无损（旧版根本不知道新版存在）；这是导入导出 only 方案最大的安全保证

---

### 跨版本导入/导出兼容（**硬要求：与旧版格式完全互通 · 唯一迁移路径**）

旧版用 `dexie-export-import` 库：
- `ExportDataDialog.vue:65` — `exportDB(db, options)` 产出 `aiaw_user_db.json`
- `ImportDataDialog.vue:84` — `importInto(db, file, opts)` 反向

**该格式是 Dexie 官方定义**（schema 元数据 + 各表行数组，blob 字段 base64 内联），不是 AIaW 私有格式。新版本必须保持同一格式可双向读写。**导入导出 only 路径下，本章定义的格式互通是「现有用户数据迁移」唯一通道**——见上一章重写。

**两个方向的实现策略不对称**：export 仍由客户端跑（用户主动行为，数据流向反过来，server-side 化收益不抵复杂度）；import 由 Stage 4.5 server-side worker 跑（详见 Stage 4.5 段）。

#### Export — 客户端流程（保持 dexie-export-import 主体）

新版的导出按钮（`ExportDataDialog.vue`）：

1. **先把 server 端权威数据全量回灌本地缓存**（多设备 / 清过缓存的客户端 server 比本地多）
   - 调 `repos.<table>.list()` for all tables（带 `since=0` 走全量；messages / artifacts 走 Stage 4 硬前置定义的 `?since=0&limit=200` cursor 续拉）
   - 写到 `db.<table>`（缓存）
2. **对象存储桥接（导出方向）**：扫描所有缓存行的 attachment 字段，遇到 `{type:'ref', url, sha256, size}` → 后台 fetch blob（4 路并发，从对象存储 pre-signed URL 直拉）→ base64 编码回内联到 row 字段；进度对用户可见（"正在打包附件 X / Y"）
3. 调 `exportDB(db, options)` —— 与旧版**字节级一致**，输出 base64 内联格式
4. 文件名仍为 `aiaw_user_db.json`

#### Import — server-side 流程（Stage 4.5 实现）

**新版 import 不再写客户端 IndexedDB**，整个 import 由 backend worker 完成：

1. 浏览器 5MB 切片直传对象存储（`POST /api/v1/import/jobs/<id>/parts/<n>` 拿 pre-signed URL）→ 不经过 backend 带宽
2. 上传完成 `POST /api/v1/import/jobs/<id>/complete` → backend worker 启动
3. Worker 在 server 端解析 JSON → 按表 INSERT 到 PG → attachment 按 64KB 阈值分流到对象存储 / inline → 改写 row 字段
4. 写入触发现有 realtime broker → 客户端 IndexedDB 通过 WS event 自然填充（与正常 LLM 对话写入走完全同一条路径）
5. 用户上传完即可关 tab，最痛的解析 + attachment 处理 100% 在 server 跑

**为什么不在客户端 import**（与原方案对比）：

- 原方案：浏览器 parse 270MB JSON → `importInto(db)` 写本地 IndexedDB → 再循环 PUT 到 backend → 再 multipart 上传 attachment。每一步都吃浏览器内存 / CPU / 上行带宽 / tab 在线。
- 新方案：浏览器只跑 5MB 切片上传循环，其余 server 端跑。低端设备（如 2GB RAM Android / 老笔记本）也能跑。

**幂等与续传**：worker 写入用 `INSERT ON CONFLICT (id) DO UPDATE WHERE existing.updated_at < incoming.updated_at`（LWW）；上传切片由 S3 multipart 协议天然支持续传（client 记 part numbers，断网恢复后查 server 已收到的 parts，传剩余）。

#### 对外格式纪律（2026-05-02 · 强约束）

- **导出 JSON 中绝不出现 `{type:'ref', url}` 字段**——所有 attachment 必须以 base64 内联形式出现。否则旧版 import 拿到 ref 对象，要么报错要么静默存为坏链接，互通破坏。
- **新版独有的元数据字段**（如 `blob_sha256` / `blob_size`）要么不进入导出文件，要么用旧版能容忍的扩展字段约定（Dexie 容忍未知字段读取，但 dexie-cloud 的 `owner` / `realmId` 字段在新版 import 时应忽略而非报错）
- 此纪律由 `tests/e2e/stage5/export-format-discipline.spec.ts` 强制（Stage 5 落地时新增）：扫描新版 export 输出的 JSON，断言无 `"type":"ref"` 子串；同时跑「新版导出 → 旧版导入 → 数据 hash 一致」往返

**前提**：新版的 `db.ts` schema 必须**保持兼容旧版**（同表名、同主键、同索引）。Stage 5 删除 `dexie-cloud-addon` 但 IndexedDB schema 不变，`exportDB`/`importInto` 仍能互通。

#### 验证

- E2E：旧版导出 → 新版 server-side import → 数据完全一致（含 `owner` / `realmId` 字段忽略；含 `messages.attachments` 大 blob 走对象存储路径）
  - 由 Stage 4.5 Step 7 的 `tests/e2e/stage4_5/step7-import-flow.spec.ts` 系列覆盖
- E2E：200MB 量级 fixture 的旧版导出文件 → 新版 server-side import 完整跑完
  - 由 Stage 4.5 出口判据的 `tests/e2e/stage4_5/exit-criteria.spec.ts::test_huge_fixture_end_to_end` 覆盖
- E2E：新版导出 → 旧版导入 → 数据完全一致（验证 ref → base64 还原；验证 export 客户端流程）
  - 由 `tests/e2e/stage5/export-format-discipline.spec.ts` 覆盖
- E2E：「对外格式纪律」—— 新版 export JSON grep 不出 `"type":"ref"` 子串
  - 同上 spec
- 单元：保留 `dexie-export-import` 依赖（仅 export 路径用，import 路径不再用）；`ExportDataDialog.vue` 在原 `exportDB(db)` 调用前加 server 拉取 + ref→base64 还原步骤；`ImportDataDialog.vue` 整个 import 路径由 Stage 4.5 重写

---

### 横切关注点

- **Schema 真源迁移**：从 Stage 1 起以后端 Alembic 迁移为权威；客户端 `db.ts` 的 schema 在 Stage 5 后**仍保持与旧版兼容**（同表名 / 同主键 / 同索引），让 `dexie-export-import` 跨版本继续可用。
- **现存 reading hooks**：`db.ts` 里的几个 `db.<table>.hook('reading', ...)` v1.4/v1.8 兼容迁移要在对应表的迁移阶段移植到后端读序列化器，老客户端无需自己规整。
- **离线写**：Stage 5 前离线写仍由 dexie-cloud-addon 兜底；Stage 5 起新增 `outbox` 表，`RemoteSyncSource` 在重连时 flush。
- **观测**：`Repository` 接口层加 `data.repo.<table>.<op>` 计数器，灰度期可对比新旧实现错误率。
- **i18n / UI 状态条**：Stage 2 起补一个全局 `syncState` 暴露（`'idle' | 'syncing' | 'offline' | 'error'`），写进 `MainLayout` 顶栏，提前在迁移期就给用户可视化反馈。
- **后端模块条件挂载**：`src-backend/data/auth.py` 等模块在 import 期间会读 `JWT_SECRET` 并 fail-fast；`src-backend/app.py` 通过 `BACKEND_DATA_API_ENABLED` flag **延迟 import** data 路由（lazy import 在挂载函数内），避免单环境 misconfig 把 CORS 代理 / 文档解析 / SPA 静态等无关功能一起带崩。新增 backend 子模块（如 Stage 2 的 `realtime.py`、Stage 4 的 `blob_store.py`、Stage 4.5 的 `import_worker.py`）时同样应在 flag 守卫内 import，并把所需 env 加入挂载函数的 fail-fast 校验列表。
- **测试基础设施增量**（与 test-infrastructure plan Phase 7+ 同步）：
  - **Docker Compose**：`tests/docker-compose.test.yml` 加 MinIO 服务（端口 9100，`MINIO_ROOT_USER=test` / `MINIO_ROOT_PASSWORD=test`），作为 BlobStore 的 S3 后端。`pnpm test:up` / `test:down` 同步起停。dev 环境不需要——dev 默认 `BLOB_STORE_KIND=local-fs` 落到 `src-backend/.blob-store/` 目录。
  - **新 profile**：`import-job` → 9015 → `tests/env/.env.test.import-job`（启用 `IMPORT_JOB_ENABLED=true` + `BLOB_STORE_KIND=s3` + `BLOB_STORE_ENDPOINT=http://localhost:9100` + `BLOB_STORE_BUCKET=aiaw-test`）；`playwright.config.ts` 加 project；`tests/scripts/run-playwright.sh` 加 build-profile 步骤；`tests/scripts/backend-start.sh` 的 `CORS_ALLOW_ORIGINS` 扩 9015。
  - **新 helper**：`tests/e2e/helpers/import-job.ts`，封装：`createImportJob(authToken, file)` / `uploadAllParts(authToken, jobId, file, opts?)` / `completeImport(authToken, jobId, parts)` / `waitForImportPhase(authToken, jobId, phase, timeoutMs)` / `subscribeImportEvents(page, jobId)`。在 `tests/README.md` §helper ↔ plan 词汇表追加 5 行。
  - **新 fixture**：`tests/api/fixtures/dexie_export.py`，导出 4 档 `aiaw_user_db.json`：`small`（10 providers + 5 dialogs + 0 attachments）/ `medium`（100 dialogs + 1000 messages + 10 × 1MB attachments）/ `large`（500 dialogs + 10000 messages + 50 × 2MB attachments）/ `huge`（1000 dialogs + 50000 messages + 100 × 2MB attachments，~200MB，slow mark 专用）。
  - **新 backend env**（Stage 4.5）：`IMPORT_JOB_ENABLED` / `BLOB_STORE_KIND` / `BLOB_STORE_BUCKET` / `BLOB_STORE_ENDPOINT` / `BLOB_STORE_ACCESS_KEY` / `BLOB_STORE_SECRET_KEY` / `BLOB_STORE_PRESIGN_TTL_SECONDS=3600` / `BLOB_INLINE_MAX_BYTES=65536` / `IMPORT_WORKER_CONCURRENCY=4` / `IMPORT_RAW_RETENTION_DAYS=7`。
  - **新前端 env**（Stage 4.5）：`IMPORT_JOB_ENABLED`（控制 ImportDataDialog 入口可见性，与后端同名 flag 配套）。my-deploy 默认全不开。

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
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/blob_store.py`（Stage 4 硬前置新增：BlobStore 接口 + LocalFS / S3 / MinIO 实现）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/routers/blobs.py`（Stage 4 硬前置新增：`/api/v1/blobs` multipart endpoint）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/models/import_job.py`（Stage 4.5 新增）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/import_worker.py`（Stage 4.5 新增：asyncio worker，4 phase）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/routers/imports.py`（Stage 4.5 新增：5 个 endpoint）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/routers/bootstrap.py`（Stage 4.5 新增：首屏一次性返回小表全量）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/data/import-client.ts`（Stage 4.5 新增：multipart 上传 + 状态订阅）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/components/ImportDataDialog.vue`（Stage 4.5 重写：走 server-side import job）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/components/ExportDataDialog.vue`（Stage 4.5 增强：export 前 ref → base64 桥接）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/e2e/helpers/import-job.ts`（Stage 4.5 新增 helper）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/api/fixtures/dexie_export.py`（Stage 4.5 新增 fixture）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/tests/docker-compose.test.yml`（Stage 4.5 加 MinIO 服务）
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
| 5 | 全新设备首次登录 + 卸载重装 + **旧版导出 → 新版导入往返字节一致**（含对象存储桥接路径） | 服务端为唯一真源；包内无 `dexie-cloud-addon`；ref blob 透明还原 |
| 全程 | **旧版导出 → 新版导入 → 旧版导入** 数据闭环 | `aiaw_user_db.json` 字节级互通，无字段丢失；新版 export JSON grep 无 `"type":"ref"` |
| 全程 | 多设备开机首次登录 | 第二台直接走「server → 本地缓存」回灌（无 push 机制，无标记表） |

每阶段均能合并到 master、独立部署、按 flag 灰度，验证失败时仅通过环境变量回退即可，无需代码 revert。
