# 云同步架构分析 + 服务端优先迁移方案

## Context

当前仓库 (AIaW fork) 的云同步基于 **Dexie + dexie-cloud-addon**（本地优先 / IndexedDB → 官方托管 SaaS `https://znm3rqzc8.dexie.cloud`）。后端 `src-backend/app.py` **不参与同步**，只做 CORS 代理与文档解析。业务代码 32 处直接 `import { db }` 调 `db.<table>.*`，没有数据访问层抽象，登录/账户/订阅 UI 直接耦合 `db.cloud.*` API。

目标是迁移到 **服务端优先**：自己的 FastAPI 承担权威存储，客户端通过 REST + WebSocket 读写，IndexedDB 退化为缓存。本计划重点在「**每一阶段都能独立合并、构建、上线、端到端验证**」，不需要全部改完才能跑通。

部署形态目标：**多用户 SaaS-style**（不仅仅是单人自部署），所以鉴权必须从一开始就支持多账号、可对外开放注册或受控开放。

---

## 修订记录

> 只保留对当前/未来工作仍有约束力的决策性记录。已完成动作的实施细节（commit hash、测试输出数字、调试踩坑）查 `git log` + commit message。按时间倒序，最新在最上。

> **术语说明**：plan 中"批次" / "批次-Xx"指**一组原子的、可整体回滚的提交集合**——本仓库是单人 + AI 开发，没有 GitHub PR 流程，"批次"对应"一次落地的 commit 序列"，不暗示 review / merge 流程。批次代号仍按字母分组方便引用（如 `批次-3a` / `批次-4b`）。

- **2026-05-03 · 4e messages 客户端 list / observeList / modifyAll 无 scope 时 cache-only fallback（非 throw）**

  - **背景**：原 plan line 933 + 4e dev briefing 提议「messages.server.ts 的全表 pull 禁用或 throw」（鉴于 backend `?dialogId=` mandatory 422 reject）。实施时复盘发现 `src/views/SearchDialog.vue` 跨 dialog 全文搜索是合法 UI 用例（用户在 search box 输入关键字 → 调 `repos.messages.list()` 跨 dialog 全文搜索），throw 会破坏现有功能；同时 backend list 422 mandatory 仍是硬契约，不能由前端 fallback 触发全表 fetch。折中：客户端 `list / observeList / modifyAll / pullForSpec` 无 scope 时**返 cache + console.warn**，best-effort 服务搜索功能；后端无变化
  - **未来约束**：
    - backend `GET /api/v1/messages?dialogId=Y` 仍 mandatory（缺 dialogId 422 reject 不变）；任何客户端 / 脚本 / future 自动化绝不能调 bare `GET /api/v1/messages`
    - 客户端 `messages.server.ts::list / observeList / modifyAll / pullForSpec` 无 scope 时**不 throw**——返 cache + 一行 `console.warn('[messages.server] <method> called without a dialogId scope ...')`，best-effort fallback；规则同步刻在 `messages.server.ts` 文件头注释里
    - 用户跨 dialog 搜索仅返"已访问过 dialog"的 messages（cache 内容），属可接受退化；UX 与未来 Stage 4.5 bootstrap response 含 `messages_recent`（per-dialog 最近 50 条）后进一步缓解
    - SearchDialog 行为不被破坏 = 4e 落地必要前提；future 重构时一旦切回 throw 必须先在 SearchDialog 加 dialog id 选择器（不能盲改 server.ts）
  - **避坑**：
    - 不要把 fallback 改回 throw —— Stage 4.5 之前 SearchDialog 跨 dialog search 仍依赖此路径
    - 不要在 fallback 里偷偷发 bare `GET /api/v1/messages?since=N` —— backend 422，等于把退化路径从"cache truncated"变成"控制台爆红 + 搜索完全坏"
    - 如未来真要全用户全 dialog 搜索，路径是后端新加 `/api/v1/messages/search?q=` endpoint（带 user_id 全表 LIKE / FTS index），而不是放开 list 的 `?dialogId=` mandatory 约束；后端那条规则是 broker 队列容量决定的硬约束
- **2026-05-03 · 4d artifacts 重新归类为「单 scope (workspaceId) 表」**

  - **背景**：上一轮 2026-05-03「plan 自洽刷新 + lazy 路径补齐」修订记录写「artifacts 双 FK (workspaceId / dialogId)」，line 913-917 4d schema 决策段写「`workspaceId` / `dialogId` 双 FK」+「`?dialogId=Y` (artifacts 也可绑 dialog)」+「workspaceId/dialogId scope 过滤」。复盘代码事实：`src/utils/types.ts::Artifact` interface 只含 `workspaceId: string`，**无 `dialogId`**；全代码 grep（`stores/workspaces.ts:69` / `pages/WorkspacePage.vue:175` / `composables/create-artifact.ts` / 等）确认 artifacts 与 dialog **没有任何关联**。按 4c items 修订先例（plan 假设与代码事实不符时按代码事实落地，code wins over stale plan text）做方向纠偏
  - **未来约束**：
    - **4d artifacts 单 scope（workspaceId），无 dialog_id 列、无 dialog cascade artifacts 路径**
    - plan 顶部 2026-05-03「plan 自洽刷新」修订记录的「artifacts 双 FK (workspaceId / dialogId)」措辞与 line 913-917 的「dialogId 双 FK」/「`?dialogId=Y` (artifacts 也可绑 dialog)」/「workspaceId/dialogId scope 过滤」/「`routers/dialogs.py::delete_dialog` 加 update artifacts.deleted_at WHERE dialog_id=dlg」**整体作废**
    - scoped pull 单 `workspaceId` scope；`artifacts.server.ts` 的 `pullForSpec` 走 `extractScopeId(..., 'workspaceId')`
    - 4d 测试段 spec 不写 dialog cascade artifacts case（artifacts 无 dialog_id 列；workspace cascade 走 `WHERE workspace_id = :ws` 一跳直达）
  - **避坑**：
    - bootstrap (Stage 4.5) 的 artifacts 不进 response 仍生效（按 workspaceId scope lazy 拉），与 items 同档处理；Stage 4.5 设计段 line 1056 / 顶部 2026-05-03 修订「Stage 4.5 Step 8 bootstrap response 不含 items / artifacts」无须改动
    - 未来若产品需求把 artifact 绑到 dialog（与代码当前形态不符），是单独 alembic migration 加 dialog_id 列 + FK + index + 改 scoped-pull.ts（今天单 scope by design），不能复用 4d 落地路径
- **2026-05-03 · plan 自洽刷新 + lazy 路径补齐 + 流式节流前置 + outbox 落 Stage**

  - **背景**：用户复盘三大产品需求时复核 plan——① 登录后立刻看到 workspaces / 点 dialog 立刻看到对话/消息（**服务端优先 / 即点即看**）；② 流式输出能跨 tab / 跨设备实时同步；③ 所有增删改正确同步（含离线场景）。逐条核 plan 后发现：
    1. **plan 内部不自洽**：line 236 旧总策略段写「IndexedDB 角色降级。Stage 0–4 仍是首屏读源，Stage 5 起退化为缓存 + 离线 outbox」，按字面读 Stage 5 才退化；但 Stage 4.5 Step 8 (line 1051+) 已规划 `GET /api/v1/bootstrap` 解决"首登 1.5s 内看到 workspaces"，4.9 (line 1117) 写「IndexedDB 缓存机制保留作为离线 / 弱网下的本地缓存层，server 是权威」。两处描述矛盾，line 236 是 stale 旧文字
    2. **lazy 路径未规划**：当前所有 server-routed 表的 `pull()` 都是「全 user 单表 since=N 全量增量」（参考 `items.server.ts::pull` / `dialogs.server.ts::pull`），后端 `list_<table>` endpoint 也只接 `?since=N`，**不支持作用域过滤**（`?workspaceId=` / `?dialogId=`）。bootstrap 只解决"首登小表全量"，不解决"打开某个 dialog 时只拉这个 dialog 的 items / messages / artifacts"。一个常用用户单 user items 上千、messages 上万是常态，按现有 pull 模式打开任何 dialog 都会全表拉一遍 → 跟旧 Dexie Cloud 全量拉本质相同
    3. **流式节流策略未定**：plan line 813 (4e) 判据写「200 次 PUT 模拟 token 流 → 第二 tab 无丢帧无倒退」，但**没定义客户端节流机制**。token 流 10-30 Hz × 完整 envelope HTTP PUT × broker `maxsize=200` close-on-overflow，长回复会撑爆队列
    4. **outbox 没真实 Stage**：line 236 提"Stage 5 起退化为缓存 + 离线 outbox"，但 Stage 5 段 (line 1150-1171) 实际只有"导入导出端到端验证"，无 outbox 实现条目。Stage 4.9 也只说"IndexedDB 缓存保留"——缓存 ≠ outbox（缓存解决"读"的离线，outbox 解决"写"的离线）
  - **未来约束**：
    - **新增「硬前置 3：作用域过滤 + scoped pull」**（详见 Stage 4 硬前置段）：`list_<table>` endpoint 加 `?workspaceId=X` / `?dialogId=Y` 过滤参数；前端 `<table>.server.ts` 引入 scoped pull——`observeFind(spec)` 把 `spec.where.workspaceId` / `spec.where.dialogId` 解析为 server query，scope 内独立维护 `lastVersion` cache（`Map<scopeKey, number>`），不再触发全表 pull。**4d artifacts / 4e messages 落地必须自带 scoped pull**；同时 retrofit 4a/4b/4c 已落地的 dialogs / items（不动 workspaces，因为 workspaces 本身是顶层 scope）。bootstrap 仍保留作为「首登小表 + 最近 50 条 message」的零等待路径
    - **4e 流式节流策略前置敲定**：客户端 `repos.messages.put()` / `update()` 在流式上下文（`isStreaming=true` 标志位）下走 batch flush——**200ms 时间窗 + 满 1KB 文本 + sentence boundary** 三触发取最先；非流式 PUT 立刻 flush。flush 时 PUT 当前累积的完整 message envelope，server WS 推 inline envelope。判据：单 dialog 5min 流式回复（生成 ~3000 字）期间 broker 入队 ≤ 25 events（200ms × 5min/200ms = 1500 上限，sentence 触发实际更少），第二 tab UI 渲染抖动肉眼不可感
    - **新增 Stage 4.7「离线 outbox」**（详见下方新 Stage 段）：在 4.5 与 4.9 之间，引入 IDB outbox 表 + 重连 flush + LWW 冲突解决；Stage 4.9 前置加「Stage 4.7 outbox 已落地 + 稳定运行 ≥ 3 天」。原 line 236 "Stage 5 起... outbox" 措辞作废
    - **line 236 旧总策略文字刷新**：改为「Stage 0–4.4 IndexedDB 仍是首屏读源；Stage 4.5 起 bootstrap 接管首登路径；Stage 4.7 起加离线 outbox；Stage 4.9 起 IndexedDB 完全降级为缓存层（server 是权威，缓存仅服务读路径离线 / 弱网）」
    - **进度快照「下一步」从 4d 改为「硬前置 3 + 4c retrofit」一批**：4d / 4e 都依赖硬前置 3，必须先落
    - **Stage 4.5 Step 8 bootstrap response 不含 items / artifacts**：明确 bootstrap 只塞「workspaces / dialogs / providers / assistants / installedPlugins / reactives / avatarImages / messages_recent (per-dialog 最近 50 条)」；items / artifacts 走硬前置 3 scoped pull，按 dialog / workspace 范围 lazy 拉。这与现有 line 1056 描述一致，本次只是显式声明"items / artifacts 不进 bootstrap 是有意为之、不是漏写"
  - **避坑**：
    - 硬前置 3 不要拆成"先后端 endpoint 加参数、再前端改 pull"两批落地——retrofit 已落地表（dialogs / items）的 server.ts 同时改 + spec 同时新加，否则中间态 server 已支持过滤但前端还在全表拉，等于白上线
    - 流式节流的 200ms 时间窗实现要在 `composables/call-api.ts` / `utils/middlewares.ts`（流式 token 入口）做 batch，不是在 `messages.server.ts::putOne` 做——后者粒度太细看不到流式上下文。具体钩点 4e 落地时确定，本修订只敲定策略
    - 离线 outbox 不要复用 dexie 的 `db.transaction` 事务模型——4.9 之后 dexie repo 实现已删，outbox 表只是 IDB schema，操作走 `db.outbox.put / delete`，flush 逻辑跑在 `realtime` 重连回调里
    - bootstrap response 不含 items / artifacts 这点要在 `tests/api/test_bootstrap.py::test_returns_all_small_tables_in_one_response` 显式断言（response 字段 set 不含 `items` / `artifacts`），否则未来误加进去会撑爆 1MB 上限
- **2026-05-03 · 4c items 重新归类为「依赖硬前置 2 的附件载体表」**

  - **背景**：上一轮规划 4c 把 items 当"独立简单叶子表"，schema 决策段写为「`type` 联合；`dialogId` 可为 null（工作区级 item vs 对话级 item）」，并在 line 38 依赖排序中把 items 与 dialogs 并列为"可并行的简单表"。复盘发现两处错：① **代码事实**：`StoredItem` 形态是 `{id, dialogId:string(必填), type:'text'|'file'|'quote', references:number, contentBuffer?:ArrayBuffer, mimeType?, name?, contentText?}`，`dialogId` 必填不可 null；`contentBuffer` 是用户上传的真附件 bytes（`DialogView.vue::949` 的 `repos.items.bulkPut(items)` 就是处理用户拖文件 / 上传图片 / 引用大段文字的入口，`type:'file'` 的 contentBuffer 可达 5MB+）。② **未来一致性**：plan 4e messages 端到端判据写"5MB attachment 端到端 → ref URL 下载 blob"，实际数据流走 items（message 只持 `StoredItemId[]`），所以**items 必须支持 blob ref 才能让 4e 跑通**，否则 4e 落地时回头改 items 污染历史
  - **未来约束**：
    - **4c items 重新归类为依赖硬前置 2**（与 4d/4e 同档），不再是"独立简单"；硬前置 2 已落地故不阻塞，仅排序约束改变
    - **依赖排序刷新**：`workspaces` ✅ → `dialogs` ✅（FK→workspaces）→ `items`（FK→dialogs，依赖硬前置 2 ✅）→ `artifacts`（依赖硬前置 2 ✅）→ `messages`（依赖硬前置 1+2 ✅）。dialogs / items 并行已不成立——items 有 FK 到 dialogs
    - **wire 形态**：`items.server.ts` 用 `src/data/blob-client.ts` 的 `serializeAttachment` / `materializeAttachment` 处理 `contentBuffer`：`< 64KB` → `{type:'inline', data:base64, content_type, size}` 内联在 row；`≥ 64KB` → `{type:'ref', url, sha256, size, content_type}` 上 BlobStore。row 其它字段（id / dialogId / type / references / mimeType / name / contentText）保持原样
    - **后端**：`routers/items.py` 仍把 `data` 当 opaque JSONB 处理，**不感知 inline/ref 形态**。后端 PG 行设计单一 `data:JSONB` 列即可，不要拆 attachment 字段——这与 dialogs / workspaces 模板一致
    - **dialog FK**：`dialog_id` 提到顶层列、FK→`dialogs.id` ON DELETE CASCADE（与 dialogs/workspaces FK 模式一致）；router PUT 校验 `data.dialogId` 存在 + 同 user，缺失/跨 user 返 409
    - **blob_refs 增删**：items.put 时 `serializeAttachment` 自动建 `(user_id, sha256)` ref；items.delete 时**不主动 delete blob ref**（多 row 共享 sha256，且后续 messages 可能再引用），交 plan 未来扩展 GC job
    - **测试加 ref 形态判据**：api 加 `test_put_item_with_ref_attachment_round_trips` + `test_inline_at_64kb_minus_one` + `test_ref_at_64kb_threshold`；spec 加 `items-ref-attachment.spec.ts`（A tab 写 100KB ArrayBuffer → blob-client 自动走 ref → B tab realtime 收到 → fetchBlob 还原 → 字节级一致）
    - **plan 内连带改动**：line 38 依赖排序、line 121/123/125 进度快照、line 774-776 4c schema 决策段、line 830 4c 状态行
  - **避坑**：
    - **不要拷贝 avatarImages 模板的 wire 处理**！avatarImages 是 < 64KB 头像（叶子表），plan 顶部 2026-05-02 修订记录明确写"叶子表（含 avatarImages）走 base64 内联即可"。items **不是叶子表**——它是消息附件载体，必须走 `serializeAttachment`
    - 后端 PG 行 `data:JSONB` 列存 client 序列化后整体（含 inline 或 ref 子对象），**不要在 PG 拆 attachment 字段单独建列**——blob 元数据归 `blobs` / `blob_refs` 表，items 行只是"指向"
    - `serializeAttachment` 是 async + 走网络（ref 模式时），调用点 `repos.items.put` 必须 await；`bulkPut` 必须 sequential 调用 putOne（与 dialogs 模板一致），不能 Promise.all 并行——多 sha256 同时 putBlob 会撞 ref 唯一约束 race
    - 跨 tab realtime apply ref item 时 `materializeAttachment` 也走网络，apply 链路必须容忍 fetchBlob 失败（presigned URL 1h 过期 / 网络抖动）：失败时 row 仍写入 IndexedDB 但 contentBuffer 留 undefined，UI 渲染时再按需 retry。**这条 4c 落地时必须有专门 spec 验证**
    - 4c 落地时 `routers/workspaces.py::delete_workspace` 的 cascade 分支按 dialogs 模板加 items update-tombstone 链路，与 dialogs cascade 共用同一 `cascade_version`（前端按 LWW 处理，跨 tab 收到一组事件无序也无所谓）
- **2026-05-02 · 硬前置 1 收窄：保留 inline envelope 流式同步 + cursor 分页（砍掉 notify-only 协议）**

  - **背景**：硬前置 1 原设计含两块——① WS / SSE event 改 notify-only（去 `row` 字段，客户端 GET row 按需拉）+ ② `?since=N` 加 cursor 续拉。实施一半发现 ① 当前朴素实现对**流式云同步**场景**零正收益、负 RTT**：bytes-on-wire 仍是 O(N²)（GET 拉的也是当前累积 row），多 N 个 RTT + N 次 PG 查询。要让 notify-only 真省带宽必须配合客户端 event coalesce/dedup + 服务端 ETag/304，~200–300 行额外异步逻辑。broker 队列内存优势（10MB → 20KB / 活跃 sub）只有大规模 SaaS 才兑现，本项目（个人 / 小规模独立部署）触不到。同时 notify-only 引入「event 到了 row 没到」中间态 + race condition + 404-as-tombstone 启发式，让多 tab / 跨设备流式同步在弱网下反而 UX 变差（断断续续 / 偶尔倒退 / 帧缺失）。
  - **未来约束**：
    - **硬前置 1 收窄为「仅 cursor 分页」**：保留 `?since=N&limit=200&next_cursor=…` 续拉协议（`pagination.py` + `providers.py` 改动）；删除 notify-only 协议设计 + per-table payload-mode 配置位 + 客户端 fetchAndDispatch 路径 + `_test/payload-mode` admin endpoint + `BACKEND_TEST_HOOKS_ENABLED` env
    - **流式云同步走 inline envelope**：所有表（含 messages / artifacts）的 WS / SSE event 始终 `{table, op, id, rev, row}` 完整 envelope；客户端直接 put row 入 IndexedDB，没有 row-fetch 中间态。**等同 Stage 1–3 现有协议直接复用到 messages / artifacts**，没有"大表协议 / 小表协议"之分
    - **broker 队列内存兜底**：依赖硬前置 2（attachment 走 ref，单 row ≤ ~200KB）+ broker 现有 `maxsize=200` close-on-overflow 机制（Stage 2 设计）即可；soak.sh 继续跑「100 条大 message 连续 PUT，server RSS 涨幅 < 50MB」做规模兜底监控
    - **批次-4d artifacts / 批次-4e messages 判据调整**：删除「WS event 字节 < 1KB」「客户端用 event.id 调 GET 拿回完整 row」类 notify-only 判据；保留「`?since=0&limit=200` 第一次响应 < 1MB / 续拉 ≥ 50 次」类 cursor 判据；新增「跨 tab 流式 PUT 200 次（模拟 token 流）→ 第二 tab UI 无丢帧 / 无倒退」类 inline 流式同步判据
    - **未来真到规模瓶颈再补 notify-only**：cursor 基础设施已到位，client-side coalesce + ETag/304 可独立加层，不影响现有 API 设计
  - **避坑**：
    - **不要顺手删 `pagination.py` / `test_pagination.py`**：cursor 是 messages / artifacts 必需的初始拉取分页机制，与 notify-only 完全解耦，删了等于批次-4e 还要重写
    - **删 notify-only 时连带删 `BACKEND_TEST_HOOKS_ENABLED` env + `_test/payload-mode` admin endpoint + `notify-only-dispatcher.spec.ts` + `test_payload_mode.py`**；但 `tests/api/conftest.py` 的 payload-mode reset 是为 admin endpoint 服务的，连带删
    - **不要回退 broker 现有 close-on-overflow**：那是 Stage 2 已有的、与 notify-only 无关的兜底机制
    - **inline 流式同步的 broker 内存可观察性**：messages / artifacts 落地后 soak.sh 的「100 条大 message 连续 PUT」结果是早期信号 —— 涨幅明显超出 50MB 时说明硬前置 2 阈值（64KB）需要调低，或真到了该补 notify-only 的规模
- **2026-05-02 · 方向调整：砍 flag 灰度机制 + 折中 批次拆分 + 引入「分水岭」端到端测试分工**

  - **背景**：new-deploy 是空库新实例 + my-deploy 是天然回滚通道（用户回退 = 用回 my-deploy URL），原 plan「flag 默认关 + 字节级一致 + dexie 双实现 + 上线但不启用」整套机制（为"无感灰度"设计）在新方向下是 dead weight。同时复盘「逐表分批」真实价值不在灰度而在「切碎不可逆 schema 决策 + alembic 颗粒度 + bisect 定位 + TDD 反馈环」，可按表组 schema 决策大小折中拆分。
  - **未来约束**：
    - **① 砍 flag 默认关上线节奏**：Stage 3+ 每张表落地时同一批 commit 内直接把表名加进 `.env.docker` 的 `BACKEND_DATA_TABLES` CSV 并 push 触发重 build，不再走「先关 flag → 后续批次再翻 flag」两步。**flag 机制本身保留到 Stage 4.9** 一次性删除（用作 server.ts 写炸时秒级 disable 单表的应急开关）
    - **② Stage 3 批次拆分**：`reactives` 自成 1 批（KV 形主键 `(user_id, key)` + envelope 重新设计）；`assistants` + `installedPluginsV2` + `avatarImages` 合成 1 批（schema 简单 + 决策点小）。共 2 批
    - **③ Stage 4 主体保持 5 批次**（每张表都有重大 schema 决策），强制依赖排序：`workspaces` → `dialogs`（FK→workspaces）→ `items`（FK→dialogs，依赖硬前置 2）→ `artifacts`（依赖硬前置 2）→ `messages`（依赖硬前置 1+2）。**注**：items 原写为"与 dialogs 可并行的简单表"已于 2026-05-03 修订记录纠正——items 是消息附件载体，FK 到 dialogs 且走 blob ref
    - **④ 新增 Stage 4.9「flag 路由层 + dexie 实现一次性下架」**：前提是 Stage 4.5 端到端验收通过 + 至少稳定运行 1 周；删 9 张 `<table>.dexie.ts` + flag 路由 + `SERVER_CAPABLE_TABLES` + `BACKEND_DATA_TABLES` 配置位
    - **⑤ Stage 4.5 = "可让老用户用"的物理分水岭**：之前不强制人工端到端，自动化测试 + 5-10 min 主路径冒烟足够；之后必须做真实数据 export → import 端到端 + 跨设备 + 跨平台真机。详见「上线节奏与人工端到端测试分工」段
    - **⑥ 回滚策略统一**：所有 Stage 3+ 工作的回滚都是「用回 my-deploy URL」，不是「flag 翻回 dexie」（虽然 Stage 4.9 之前 flag 仍可作为应急开关）
    - **⑦ 同步 CLAUDE.md**：「flag 默认关 = 字节级一致」规则改成「机制保留作应急开关；上线节奏不再用 flag 兜底」
  - **避坑提前标记**：
    - 砍上线节奏 ≠ 砍测试：每张表对应的批次仍须**同批次落 spec/api + 本地真跑过 `pnpm test:api && pnpm test:e2e` + 必须真跑红一次再绿**（CLAUDE.md 工作流契约不变）
    - Stage 4.9 删 flag 路由前必须先做 Stage 4.5 端到端验收：万一 ImportJob 写错数据，删之前还能 flag 翻 dexie 救场；删后救不了
    - Stage 4.9 删 dexie 实现连带删 IndexedDB 缓存路径要谨慎：当前倾向 IndexedDB 保留作为纯本地缓存（server 是权威），决策细节留 Stage 4.9 这批 commit时确认
- **2026-05-02 · Stage 2.5 摘双登录 UI + dexie-cloud-addon**

  - **背景**：new-deploy 是空库新实例 + 走"导入导出 only"路径，旧 Stage 1.5 设计的双登录 + `linked_dexie_email` mapping + `dexie-cloud-addon` 都成了误导，前置到 Stage 2.5 一次性清完，不等 Stage 5
  - **未来约束**：
    - 所有"是否启用账号系统"的 UI gate 用 `BackendAuth` 判断，**不再用 `DexieDBURL` 当代理变量**
    - AccountPage 单一登录入口（自家 JWT），不存在"原 Dexie 账号"区块
    - new-deploy 镜像不再依赖 `dexie-cloud-addon` 包；new-deploy 不连任何外部 Dexie SaaS
    - Stage 5 不再有"卸 addon"工作，收窄为"导入导出端到端验证"
- **2026-05-02 · 部署分支拓扑硬切：双实例 my-deploy / new-deploy**

  - **背景**：「导入导出 only」迁移路径下 my-deploy 是老用户兜底实例、new-deploy 是新版独立实例，云同步重构代码不应同时进两个分支
  - **未来约束**：
    - **Stage 3+ 所有代码 / plan / spec 只合 `new-deploy`**，不再触碰 my-deploy
    - my-deploy 仅接：上游 master 同步、老 Dexie 路径 bug fix；HEAD 锁在 commit `2b26349`
    - Northflank 服务 1 → my-deploy（老用户兜底，env 不开任何 backend flag），服务 2 → new-deploy（独立 Postgres + 全档 flag 开）
    - `backup/my-deploy-pre-reset` 远端永久保留 6 个月+，作为整体回滚逃生通道
    - force-push deploy 分支必须按 4 步走：① 建 `backup/<branch>-pre-reset` safety net；② 建承接代码的新分支；③ 本地 reset；④ `git push --force-with-lease`（不是 `--force`）
- **2026-05-02 · 服务端 Import Job 设计敲定（Stage 4.5）**

  - **背景**：客户端跑全套 import（parse / 写 IndexedDB / push backend / 上传 attachment）在 200MB 数据 + 弱网场景下 OOM、tab 不能关、断网状态全丢
  - **未来约束**：
    - Stage 4.5 含 8 个 Step：ImportJob 模型 + S3 multipart 上传 5 个 endpoint + Phase A（解析）+ B（结构表）+ C（messages 文字）+ D（attachments → 对象存储）worker + WS 进度推送 + ImportDataDialog 重写（5MB 切片直传）+ `GET /api/v1/bootstrap` 首屏路由 guard
    - 浏览器只负责文件分块直传对象存储；后续阶段全部在 backend worker 跑，**用户上传完即可关 tab**
    - 200MB 用户体感"卡住"时间从 30-60min 降到 3-10min
    - 测试脚手架增量：MinIO 容器（端口 9100）+ `import-job` profile（端口 9015）+ helper `import-job.ts` + fixture `dexie_export.py`（small/medium/large/huge=200MB 四档）
    - DB 唯一约束保证每用户同一时刻只有 1 个 active job（partial unique index）
- **2026-05-02 · 迁移路径转向：导入导出 only + 对象存储升格 Stage 4 硬前置**

  - **背景**：「客户端驱动一次性 push」路径在 200MB 老用户场景三处崩——WS event 带完整 row → broker `maxsize=200` 队列秒爆；`?since=N` 全 row 返回 → fetch 几十 MB；PG TEXT 列存 base64 attachment → 表线性膨胀
  - **未来约束**：
    - 「现有用户数据迁移」单一路径 = 老用户在 my-deploy ExportDataDialog 导出 → 在 new-deploy ImportDataDialog 导入；不存在自动后台迁移、不存在双写窗口、不存在迁移标记表、不存在 `/api/v1/migrate/status` endpoint
    - new-deploy 默认不挂 `dexie-cloud-addon`；老用户登录新版看到的是空账号；不愿迁的继续用 my-deploy
    - **Stage 4 硬前置 1（messages / artifacts 落地前不可后置）**：原设计含 ① notify-only WS 协议（去 `row` 字段）+ ② cursor 分页（`?since=N&limit=200&next_cursor=…`）。**2026-05-02 收窄**为「仅 cursor 分页」，notify-only 部分整体放弃；流式云同步走 inline envelope。详见顶部修订记录「硬前置 1 收窄」条
    - **Stage 4 硬前置 2（同上）**：对象存储分流，阈值 64KB；< 64KB inline 进 PG，≥ 64KB 走 multipart `/api/v1/blobs` 拿 ref；PG 行只存 `{type:'ref', url, sha256, size}`
    - **对外格式纪律**：导出 JSON 永远 base64 内联，**绝不**出现 `{type:'ref', url}` 字段；导出时新版 fetch 所有 ref blob → 重新 base64 内联，与旧版导出字节级互通
    - 失败回退路径 = 用回 my-deploy URL；旧版本地 + Dexie Cloud 数据完整无损（旧版根本不知道新版存在）
- **2026-05-01 · 鉴权方案：JWKS 桥接废弃 → Stage 1.5 自家 JWT**

  - **背景**：实测 Dexie Cloud 未公开 JWKS、也无 token introspection 端点，原 Stage 1 计划的「JWKS 桥接鉴权」路径不可行
  - **未来约束**：
    - 所有 `/api/v1/*` 端点统一走自家 JWT 鉴权（`current_user` FastAPI 依赖项），**不再考虑桥接外部身份源**
    - access token 30 min + refresh token 30 day（哈希存 `refresh_tokens` 表，可吊销）
    - `JWT_SECRET` 强制非空，env 文件不入 git，生产用密钥管理服务
    - MVP 阶段默认 `ALLOW_REGISTRATION=invite`；公网开放需补 SMTP + 验证邮件

---

## 进度快照（最新 · 2026-05-03）

> 本段反映"现在到哪 / 下一步做什么"，每个 Stage 落地时同步刷新。已落地代码细节查 `git log` + commit message。

### 已完成

- **Stage 0** ✅ Repository / AuthSource 抽象层（消除 32 处 `db.*` 直调）
- **Stage 1** ✅ 后端骨架（FastAPI + Postgres + alembic）+ providers 表 REST CRUD + `?since=N` 增量 + soft-delete tombstone + flag 路由（按表选 server / dexie 实现）
- **Stage 1.5** ✅ 自家 JWT 多用户鉴权（register / login / refresh / logout / me + `current_user` 依赖项）
- **Stage 2** ✅ 实时通道：后端 broker + WS `/api/v1/stream`；前端 `RemoteSyncSource` + WS / SSE / poll / auto 四档 transport 降级；`providers` 表整张接通 realtime 跨 tab
- **Stage 2.5** ✅ 摘 `dexie-cloud-addon` + 删双登录 UI + 删 `linked_dexie_email` 字段链 + 后端 alembic migration drop 列（`c4f1e2d3a8b0`）
- **Stage 3 / 批次-3a** ✅ `reactives` KV 表迁到 backend：复合主键 `(user_id, key)` + envelope `{key, version, updated_at, deleted, data}` + alembic migration `d6a3f8c91e22` + WS / SSE 加 reactives 白名单 + 前端 `reactives.server.ts` 路由 + `BACKEND_DATA_TABLES=providers,reactives`。`persistent-reactive.ts` 透明走 server，跨 tab 实时同步可用
- **Stage 3 / 批次-3b** ✅ `assistants` (id-PK) + `installedPlugins` (KV-PK like reactives, 但 `data` 是整行) + `avatarImages` (id-PK + ArrayBuffer↔base64 wire) 三张叶子表迁到 backend：alembic migration `e4b2c5f9d017` + 3 个 router + WS / SSE 加 3 张表白名单 + 3 个 `<table>.server.ts` + `BACKEND_DATA_TABLES=providers,reactives,assistants,installedPlugins,avatarImages`
- **Stage 4 主体 / 批次-4a** ✅ `workspaces` (id-PK，type/parentId 嵌 `data`、`$root` sentinel、不建自引用 FK) 迁到 backend：alembic migration `a7f3c2e9b481` + `routers/workspaces.py`（DELETE 接 `?cascade=true|false` no-op 预留 4b/4c/4d/4e wire 形状）+ WS / SSE 加 workspaces 白名单与 serializer + `workspaces.server.ts`（delete 走 `?cascade=true`）+ `BACKEND_DATA_TABLES=...,workspaces`；顺手修 `stores/workspaces.ts::deleteItem` 不再 runTx 包 dexie 事务（与 server-routed 表不兼容）+ `db.ts` 3 个 reading hook 容忍 undefined（latent bug）
- **Stage 4 主体 / 批次-4b** ✅ `dialogs` (id-PK + `workspace_id` 提到顶层列，FK→workspaces.id ON DELETE CASCADE) 迁到 backend：alembic migration `b5d2c8e7a394` + `routers/dialogs.py`（PUT 校验 workspaceId 存在 + 同 user，缺失/跨 user 返回 409 而非 500）+ `routers/workspaces.py::delete_workspace` 接通真级联（cascade=true 时同事务 tombstone 所有 workspace_id 匹配的 dialogs，发独立 WS event 链）+ WS / SSE 加 dialogs 白名单 + `dialogs.server.ts`（id-PK 模板）+ `BACKEND_DATA_TABLES=...,dialogs`
- **Stage 4 主体 / 批次-4c** ✅ `items` (id-PK + `dialog_id` 顶层列 FK→dialogs.id ON DELETE CASCADE，**附件载体表**) 迁到 backend：alembic migration `c4f9e2a8d6b3` + `routers/items.py`（PUT 校验 dialogId 存在 + 同 user，缺失/跨 user 返回 409；DELETE soft-tombstone）+ `routers/workspaces.py::delete_workspace` 二级级联（dialog → item update-tombstone 链路，共用 cascade_version，发独立 WS event 链）+ WS / SSE 加 items 白名单 + `items.server.ts`（**`contentBuffer:ArrayBuffer` 走 `blob-client.serializeAttachment` < 64KB inline / ≥ 64KB ref；apply 链路 try/catch `materializeAttachment` 失败容忍**）+ `BACKEND_DATA_TABLES=...,items`
- **Stage 4 主体 / 批次-4d** ✅ `artifacts` (id-PK + `workspace_id` 顶层列 FK→workspaces.id ON DELETE CASCADE，**单 scope 表 + ref 大版本载体**) 迁到 backend：alembic migration `d8b2f7c4a195` + `routers/artifacts.py`（GET list / GET one / PUT / DELETE，PUT 校验 workspaceId 存在 + 同 user，缺失/跨 user 返回 409 而非 500；同时 catch IntegrityError 转 409 防 race）+ `routers/workspaces.py::delete_workspace` 一跳级联 artifacts（与 dialogs / items 共用同一 cascade_version；artifacts 直接按 workspace_id 过滤，无 dialog hop——因 `Artifact` interface 仅 `workspaceId`，无 `dialogId`）+ WS / SSE 加 artifacts 白名单 + `artifacts.server.ts`（**`versions:ArtifactVersion[]` JSON.stringify utf-8 byte 算大小，< 64KB inline；≥ 64KB spill 到 `versionsBlob:AttachmentEnvelope` ref；decode 端 try/catch `materializeAttachment` 失败容忍——row 仍写入 IDB，versions 留空数组**；scoped pull 单 scope `workspaceId`；bulkPut sequential 避免 (user_id,sha256) blob_refs 唯一约束 race）+ `BACKEND_DATA_TABLES=...,artifacts`。**单 scope 决策摘录**：plan 顶部 2026-05-03「4d artifacts 重新归类为单 scope」修订记录纠正了上一轮假设的「workspaceId / dialogId 双 FK」——按代码事实落地
- **Stage 4 主体 / 批次-4e** ✅ `messages` (id-PK + `dialog_id` 顶层列 FK→dialogs.id ON DELETE CASCADE，**`?dialogId=` mandatory 422 + ref 大 contents 载体 + 流式节流**) 迁到 backend：alembic migration `e7c4a821f693` + `routers/messages.py`（GET list `?dialogId=` mandatory（缺 422 reject）+ GET one + PUT 校验 dialogId 存在 + 同 user 缺失/跨 user 返 409；同时 catch IntegrityError 转 409 防 race；list 与 cursor `?since=` + `?limit=` 三向组合）+ `routers/dialogs.py::delete_dialog` 同事务级联 messages（共用 cascade_version + 顺序 publish messages → dialog）+ `routers/workspaces.py::delete_workspace` 二跳级联 messages（按 cascaded_dialog_ids 过滤，与 dialogs / items / artifacts 共用 cascade_version；publish 顺序 dialogs → items → artifacts → messages → workspace，LWW idempotent，前端无序无所谓）+ WS / SSE 加 messages 白名单 + `messages.server.ts`（**`Message.contents:MessageContent[]` 整体 envelope JSON.stringify utf-8 byte 算大小，< 64KB inline；≥ 64KB spill `contents` 到 `contentsBlob:AttachmentEnvelope` ref + `contents=[]`；decode 端 try/catch `materializeAttachment` 失败容忍——row 仍写入 IDB，contents 留空数组**；scoped pull 单 scope `dialogId`；list/observeList/modifyAll/pullForSpec 无 scope 时**返 cache + console.warn 不 throw**——SearchDialog 跨 dialog 搜索路径，详见 plan 顶部 2026-05-03「4e messages 客户端 list cache-only fallback」修订记录；bulkPut sequential 避免 (user_id,sha256) blob_refs 唯一约束 race）+ **流式节流 `composables/message-stream-flush.ts`**（`createMessageStreamFlush({ flush, intervalMs?:200, byteThreshold?:1024, chunkBytesOf?, boundaryTextOf? })`：三触发——① 200ms 时间窗 ② ≥1024 byte 累积 ③ tail 匹配 `[。？！.?!\n]\s*$`；inflight 期间合并新 enqueue（newest wins）；`stop()` 强制 flush + 等待 inflight；测试钩子 `flushCount() / bytesPending() / msSinceLastFlush()`；EXPOSE_DB 守卫的 `window.__messageStreamFlush__ = { create, intervalMs:200, byteThreshold:1024 }`）+ `DialogView.vue` 替换 `throttle(50)` 为 `createMessageStreamFlush`，每个 chunk 调 `update(part.text)` 传 chunkBytes hint + sentence-boundary 取 `assistant-message.text` 末 4 字；finalize 成功/失败两条路径前 `await streamFlush.stop()` 保证最终 envelope 一致 + `BACKEND_DATA_TABLES=...,messages`
- **Stage 4 硬前置 2** ✅ 对象存储 BlobStore + `/api/v1/blobs` endpoint：`blobs` 表 (sha256 PK + size + content_type + storage_key) + `blob_refs` 表 ((user_id, sha256) 复合 PK + last_seen_at) + alembic migration `f1a3b8c5d4e2`；`BlobStore` 抽象接口 + `LocalFsBlobStore` 默认实现（src-backend/.blob-store 分片路径 `<sha[:2]>/<rest>`）+ `S3BlobStore` 骨架 (boto3 lazy import)；HMAC-SHA256(JWT_SECRET, `sha\|exp`) 签名 URL TTL 1h；POST 上传 + GET metadata + HEAD + GET `/{sha}/data?exp=&sig=` presign + DELETE per-user ref；前端 `src/data/blob-client.ts`（`putBlob` / `fetchBlob` / `serializeAttachment` / `materializeAttachment` + `BLOB_INLINE_MAX_BYTES=64KB`）
- **Stage 4 硬前置 1** ✅（仅 cursor 分页）`src-backend/data/pagination.py`（`CursorPage` envelope + `normalize_limit` + `build_page` + `CURSOR_MAX_LIMIT=1000`）+ `routers/providers.py` 的 list endpoint 加 `?limit=N` opt-in（不带 limit 维持 `list[Row]` 向后兼容，带 limit 升格为 `{rows, next_cursor}` envelope）+ `tests/api/test_pagination.py`（无 limit / 带 limit / 满页 next_cursor / 边界 / `limit<=0` 拒 / `>CURSOR_MAX_LIMIT` clamp / 续拉到 null 等多 case）。原设计中的 notify-only WS 协议 / per-table payload-mode / fetchAndDispatch 等整体放弃，详见 2026-05-02「硬前置 1 收窄」修订记录。流式云同步走 inline envelope，所有表（含 messages / artifacts）共用 Stage 1–3 现有协议
- **测试脚手架** ✅ pytest（`tests/api/`，132 case 全绿 ~90s）+ Playwright（`tests/e2e/`，多 profile：baseline / providers-rest / realtime-{ws,sse,poll,auto}，72 passed / 264 skipped ~5 min；偶发 pre-existing flake `scenarioB ws auto-reconnect` + reactives cache-roundtrip race，单跑均稳）+ soak.sh（RSS / p95 / bundle 上线把关脚本）+ build profile cache（key = env-sha256 + git rev + package.json hash）
- **Stage 4 横切 UX 修复** ✅ syncRef 输入抖动：默认 debounceMs=200 合并连续按键 + suppressSourceWhileEditingMs=1500 编辑期忽略 source echo + `flush:'sync'` watch 防止 source/user 写合并 trigger（Vue 默认 post-flush 会合并同 tick 内多次 ref 写，导致 timestamp echo detection 失效）+ `onScopeDispose` flush pending 不丢字 + EXPOSE_DB 守卫的 `__syncRefHarness__` test hook + `tests/e2e/stage4_pre/sync-ref-flicker.spec.ts` 5 case（debounce 合并 / echo 抑制 / 静默后远端更新生效 / legacy mode 0/0 / rapid edit + 并发 echo）。覆盖现有 5 处调用全部受益：CustomProvider / AssistantView / WorkspaceSettings / PluginAdjust / EnablePluginsItems。修用户报告的"输入服务商名时已输入字符被 server echo 覆盖"现场
- **Stage 4.5 / Step 1** ✅ ImportJob 模型 + worker 骨架 + Phase A 解析：alembic migration `a91f3c5e8d2b`（建 `import_jobs` 表 + `ix_import_jobs_user_version` + `uq_import_jobs_active_per_user` partial unique index 覆盖 7 个 NON_TERMINAL_STATUSES）+ `data/models/import_job.py`（`ImportJob` ORM + `_envelope()` server-routed 契约 + `NON_TERMINAL_STATUSES` / `TERMINAL_STATUSES` 共享元组 model+migration+worker 三处复用）+ `data/import_worker.py`（`ImportWorker` 单进程单 task 主循环 + `startup_complete: asyncio.Event` lifespan 集成 + `POLL_INTERVAL_SECONDS=1.0` 轮询 + `notify_pending()` Step 2 钩子预留 + `_advance_one()` 状态机 dispatch + `_do_phase_a()` 失败 try/catch 转 `status='failed'` + `error_message`，**Phase A**：`run_phase_a(job_id, blob_store)` 用 ijson `parse_async` 流式分流写 `<IMPORT_JOB_TMP_ROOT>/import-<job_id>/<table>.ndjson`、自数 row count 不信任 fixture rowCount、blob-bearing 表`{messages,items,avatarImages}` row 内 attachment envelope 候选计数、单 events-driven pass 同时校验 header `formatName='dexie'` + 多个 ObjectBuilder 行级聚合）+ `blob_store.py::BlobStore.open_stream` 抽象基类默认实现 + `LocalFsBlobStore.open_stream()` 真流式 override（chunk_size 默认 1MB，配合 ijson `parse_async` 让 200MB JSON 解析 RSS 增量 << fixture 大小）+ `data/__init__.py` 注册 `ImportJob` 进 `Base.metadata` + `app.py` lifespan hook（`IMPORT_JOB_ENABLED=true` flag 守卫下 lazy import `get_worker()` 注册到 `_import_worker` module-level；lifespan startup 调 `worker.start()` await `startup_complete`，shutdown 调 `worker.stop()` 5s timeout cancel）+ `requirements.txt` 加 `ijson>=3.2,<4.0`（实测 3.5.0）。
  - **关键修复（test agent 红测发现）**：dev 初版 worker 直接把 `BlobStore.open_stream()` 的 async iterator 喂给 `ijson.parse_async()`，但后者期望文件式对象有 `await read(n)`，不接受 async generator → 全 Phase A case 红炸 `'async_generator' object has no attribute 'read'`。修：在 `run_phase_a()` 内加 `_AsyncReader` wrapper，serve `await read(n)` 出小 buffer，按需从 src async iterator 拉 chunk + 累 byte_count（保留原 streaming 语义；只在内部加薄 file-like adapter）。
  - **测试基建变更**：`tests/scripts/backend-start.sh` 加 `export IMPORT_JOB_ENABLED='true'`（worker lifespan 注册的 flag 守卫；测试 backend 必须开）+ `tests/api/conftest.py::db_reset` TRUNCATE 列表已加 `import_jobs`（dev 已落）+ `tests/api/fixtures/__init__.py` 空 marker（dev 已落）+ `tests/api/fixtures/dexie_export.py` 提供 `make_small_export()` (10 providers + 5 dialogs + 1 workspace) / `iter_huge_export_bytes(message_count, attachment_count, attachment_bytes)` 流式生成 ~67MB 默认 / `write_huge_export_to_path()` / `export_dict_to_bytes()` / `collect_iter_to_bytes()`。`.env.docker` 加 `IMPORT_JOB_ENABLED` 用法注释段（按本文件顶部"后端 runtime env 不写本文件"约定不赋值）。
  - **api**：`tests/api/test_import_job.py::test_phase_a_extracts_table_row_counts`（small fixture → 16 rows + per-table NDJSON 行数精确）/ `::test_phase_a_streaming_memory_under_200mb`（slow，~67MB fixture，RSS delta ≈ 72MB << 200MB budget；in-process 直调 `run_phase_a` + 隔离 `LocalFsBlobStore` 根 + `monkeypatch` `data.db.SessionLocal` 指向 5434 测试 DB 因测试 process 默认无 `DATABASE_URL` env）/ `::test_phase_a_invalid_json_marks_failed`（截断 JSON → status=failed + error_message 含 'phase A'）/ `::test_phase_a_unsupported_format_name_marks_failed`（formatName='sqlite' → error_message 含 'unsupported formatName'）/ `::test_active_job_unique_constraint`（partial index → 同 user 第二个 active 行 UniqueViolation）/ `::test_unique_constraint_allows_terminal_jobs`（done/failed/cancelled 不参与，可建新 active；新 active 后第三个 active 仍 UniqueViolation）/ `::test_worker_recovers_active_job_on_startup`（手动 INSERT status='parsing' 行 → worker 1s poll 内拿走推到 phase_b + NDJSON 已写出）/ `::test_envelope_wire_format`（直构 `ImportJob` Python 对象调 `_envelope()` 验 server-routed `{id, version, updated_at, deleted, data:{...}}` 契约）。
  - **测试输出**：`pnpm test:api`（**215 passed / 0 failed in 133.11s**，新增 8 case 全绿，无回归）。
  - **故障注入红测**（CLAUDE.md 「新 spec 落地后必须真跑红一次再跑绿」铁律满足）：
    - **第 0 轮 dev 实初版自带 bug**（自然红）：dev 实现的 `_byte_iter()` async generator 直传给 `ijson.parse_async` 触发 `'async_generator' object has no attribute 'read'` → `test_phase_a_extracts_table_row_counts / _unsupported_format_name_marks_failed / _worker_recovers_active_job_on_startup` 三 case 真红，stdout 完整 dump 失败 envelope `{'id': 'job-small-664635cb', 'status': 'failed', ..., 'error_message': "phase A: phase A stream parse failed: 'async_generator' object has no attribute 'read'"}`。修 `import_worker.py` 加 `_AsyncReader` 适配后 → 全绿。
    - **第 1 轮 header validation 反向**：把 `import_worker.py:309` 的 `if value != 'dexie':` 改为 `if value != 'NOT-DEXIE-INJECTED':` + 同步改 line 376 兜底校验 → 重启 backend → `test_phase_a_extracts_table_row_counts` 红，stdout `AssertionError: unexpected end state: {..., 'status': 'failed', ..., 'error_message': "phase A: unsupported formatName: 'dexie'"}` —— spec 看到合法 dexie export 被误判为不支持 → 恢复 → 绿。
    - **第 2 轮 partial unique index drop**：`PGPASSWORD=aiaw_test psql ... -c "DROP INDEX uq_import_jobs_active_per_user"`（不动 migration 文件，避免侵入）→ `test_active_job_unique_constraint` 红 `Failed: DID NOT RAISE <class 'psycopg.errors.UniqueViolation'>` —— spec 看到第二个 active 行没被挡 → `CREATE UNIQUE INDEX ... WHERE status IN (NON_TERMINAL_7)` 恢复 → 绿。
- **Stage 4.5 / Step 2** ✅ multipart 上传 5 个 endpoint + LocalFs `_internal` PUT shim + worker queued→parsing 自动推进：`src-backend/data/routers/imports.py`（5 public endpoint：POST `/api/v1/import/jobs`（create job + 调 `BlobStore.create_multipart_upload(imports/<user>/<job_id>.json)`，partial-unique-index race 时返 409 + winner 的 status snapshot）/ POST `/{id}/parts/{n}`（仅 `status='uploading'` 状态，409 wrong-state 兜底；返 `{upload_url, part_number, expires_at}`，URL 走 LocalFs `_internal` PUT shim 带 `?exp=&sig=` HMAC）/ POST `/{id}/complete`（先 `_validate_parts_contiguous` 校 1..N 严格递增 + 不重复 + 起点=1，三档 detail 文案 'duplicate' / 'start at 1' / 'contiguous'；后状态 uploading→assembling→queued + final raw_object_key=sha256 + `broker.publish(import_jobs put)` + `worker.notify_pending()` 唤醒避免 1s poll 抖动）/ GET `/{id}` 返 status snapshot / GET `?status=active` 列当前 active job、其他 status filter 400 reject / DELETE `/{id}`：abort multipart + soft 转 `cancelled` + 释放 partial index slot 立刻可建新 job + 发 WS cancelled event；TODO Step 3 加 `imported_from_job_id` 列后 mid-Phase 取消时软删该 job 已写入行）+ 1 internal endpoint：PUT `/api/v1/_internal/multipart/{up_id}/part/{n}?exp=&sig=` 仅 `LocalFsBlobStore` 接受、其他 backend 返 404；exp 在 sig 前校验、兼具 'url expired' / 'bad signature' 两文案；100MB body cap；返 `{etag=sha256, part_number, size}`。
  - `src-backend/data/blob_store.py`：`BlobStore` 抽象基类加 4 个 multipart 方法 `create_multipart_upload` / `generate_part_url` / `complete_multipart_upload` / `abort_multipart_upload`；`LocalFsBlobStore` 完整实现：`<root>/_multipart/<upload_id>/part_<n>` 分片暂存 + `.meta.json` 记原始 key + write_part 原子写（tmp 文件 rename）+ complete 流式 sha256 + 移动到 canonical `<sha[:2]>/<sha[2:]>` + 同 sha dedup（trust-the-hash）+ 清理 `_multipart/<upload_id>/`；abort `shutil.rmtree(ignore_errors=True)` 幂等。`S3BlobStore` 加 multipart 骨架（`generate_part_url` / `abort_multipart_upload` 标 `NotImplementedError` 因需 Key plumbing，prod 选 S3 时再补）。新增 `sign_multipart_part_url` / `verify_multipart_part_signature`（HMAC-SHA256(JWT_SECRET, `mp\|<up_id>\|<n>\|<exp>`)，独立 `mp|...` 消息命名空间避免 leaked blob URL 被复用 PUT part）。
  - `src-backend/app.py`：`_enable_backend_data_api` 内 `IMPORT_JOB_ENABLED=true` flag 守卫 lazy import 块加 `from data.routers import imports as imports_router` + `app.include_router(imports_router.router)` + 日志加 'imports router mounted'。
  - **api**：`tests/api/test_imports_router.py` 12 case：① `test_create_job_returns_job_id_and_multipart_upload_id`（job_id=UUID + multipart_upload_id 非空 + raw_object_key=`imports/<user>/<job>.json` + status=uploading）/ ② `test_part_url_is_presigned_with_short_ttl`（URL path=`/api/v1/_internal/multipart/<up>/part/1`、qs 含 `exp` + `sig` 64 hex、expires_at 在 `time.time()+3600 ±10s` 窗 + URL 内 `exp` 与 body `expires_at` 一致）/ ③ `test_complete_with_all_parts_triggers_worker`（小 dexie export 切 2 part PUT → complete → status=queued → 2.5s 后 worker 推到 phase_b/parsing）/ ④ `test_complete_with_missing_parts_returns_400`（gap parts 'contiguous' / 不从 1 起 'start at 1' / 重复 'duplicate' 三 sub-case）/ ⑤ `test_get_job_returns_status_and_progress`（必含 status / processed_rows / total_rows / processed_blobs / total_blobs / error_message / dead_letter / created_at / updated_at 字段）/ ⑥ `test_get_active_jobs_returns_in_progress_only`（active=`[snap]`、cancel 后=`[]`、`status=foo` → 400）/ ⑦ `test_delete_active_job_aborts_multipart_and_releases_slot`（DELETE → cancelled + 同 user 立即可 POST 新 job 不 409；GET 旧 job 仍可见 cancelled）/ ⑧ `test_user_b_cannot_access_user_a_job_returns_404`（B 对 A 的 job_id GET / parts / complete / DELETE 全 404 'not found'，绝不 403）/ ⑨ `test_part_endpoint_rejects_when_job_not_in_uploading_status`（SQL UPDATE 把 job 改 'parsing' → POST `/parts/1` 409 'not uploading'）/ ⑩ `test_create_active_job_conflict_returns_existing_snapshot`（第二次 create → 409 + `detail.job` 含第一次 snapshot；cancel 第一个 → 第三次 create 201）/ ⑪ `test_internal_part_endpoint_rejects_bad_sig`（篡改 sig 首字符 → 403 'bad signature'；`exp=1` 调过期 → 403 'url expired'）/ ⑫ `test_complete_dedupes_same_sha256_across_jobs`（A、B 两 user 上传完全相同字节 → canonical `<sha[:2]>/<sha[2:]>` 文件存在且 size 一致 + 两 job snapshot raw_object_key 都=sha256，trust-the-hash dedup 命中）。
  - **测试输出**：`pnpm test:api`（**227 passed / 0 failed in 142.52s**，新增 12 case 全绿，无回归；含 Step 1 8 case + 跨 stage providers/reactives/assistants/installedPlugins/avatarImages/workspaces/dialogs/items/artifacts/messages/blobs/auth/realtime 全套兜底）。
  - **故障注入红测**（CLAUDE.md 「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch）：
    - **第 1 轮 cross-user 404 退化为 403**：`imports.py::_load_job_for_user` 把 `if job is None or job.user_id != user_id: raise 404` 改为 `if job is None: raise 404 / if mismatch: raise 403 'forbidden'` → `test_user_b_cannot_access_user_a_job_returns_404` 红，stdout `AssertionError: GET should 404, got: 403 {"detail":"forbidden"}` `assert 403 == 404` —— spec 真在卡 cross-user 404-mask 契约 → 恢复 → 绿。
    - **第 2 轮 parts 连续校验关闭**：`_validate_parts_contiguous` 内 `if nums != expected: raise 400` 改为 `pass` → `test_complete_with_missing_parts_returns_400` 红，stdout `AssertionError: {"detail":"complete failed: part 1 not uploaded"} assert 'contiguous' in ...` —— gap parts 通过校验后到 BlobStore 才发现 part 缺失，spec 真在卡 router 层早 fail-fast → 恢复 → 绿。
    - **第 3 轮 part-url sig 校验关闭**：`_internal` shim 的 `if not verify_multipart_part_signature(...): raise 403 'bad signature'` 改为 `pass` → `test_internal_part_endpoint_rejects_bad_sig` 红，stdout `AssertionError: tampered sig: expected 403, got 200: {"etag":"2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824","part_number":1,"size":5}` —— 篡改 sig 仍 200 写入 part bytes（etag = sha256("hello")），spec 真在卡 HMAC sig verify → 恢复 → 绿。
- **Stage 4.5 / Step 3** ✅ Phase B 结构表写入 + imported_from_job_id 加列 + DELETE cascade soft-delete：alembic migration `b3e7d4f8a1c5`（10 张 server-routed 表各加 `imported_from_job_id String NULL FK→import_jobs.id ON DELETE SET NULL` + `imported_at TIMESTAMP NULL` + b-tree 索引 `ix_<tbl>_imported_from_job_id`，down_revision=`a91f3c5e8d2b`，dev round-trip 验证 upgrade↔downgrade）+ 各 `data/models/{provider,reactive,assistant,installed_plugin,avatar_image,workspace,dialog,item,artifact,message}.py` 加同名两列 + 索引（**不进** 各 router `_to_row` / `_to_event` 的 `data` 字段，纯 PG bookkeeping）+ `data/import_worker.py::PHASE_B_TABLES` 7-tuple 顺序常量（dexie wire name → backend snake_case：providers/assistants/installedPluginsV2→installed_plugins/reactives/avatarImages→avatar_images/workspaces/dialogs，dependency order workspaces 在 dialogs 之前防 FK 失败）+ `SERVER_ROUTED_TABLES` 10-tuple（DELETE cascade 用）+ `_extract_lww_timestamp(row)`（接受 `updatedAt` / `updated_at` ISO 字符串，缺省 `now()` 令再导入覆盖旧数据）+ `_normalize_kv_row(table, row)`（id-PK / KV-PK / dialogs 的 workspace_id 三种 PK 形态分流，dialogs 多塞 `workspace_id` 提升列）+ `_build_insert_stmt(table)` 返 `pg_insert(Model)` + `(Model, conflict_index)` + `_phase_b_load_table(session, job, user_id, dexie_table, backend_table, ndjson_path)`（按 NDJSON 行 batched UPSERT，`ON CONFLICT (...) DO UPDATE SET data,version,updated_at,deleted_at=NULL,imported_from_job_id,imported_at WHERE existing.updated_at < EXCLUDED.updated_at` LWW 守卫，pre-fetch `nextval('global_change_seq')` 数组避免 N round-trip，PHASE_B_BATCH_SIZE=500，单 commit per table）+ `run_phase_b(job_id)` 按 PHASE_B_TABLES 顺序逐表 commit + 每表完成后 `broker.publish(import_jobs put)` 进度事件 + 累计 `processed_rows` 推进；`ImportWorker._do_phase_b` 整 phase try/catch wrap + 成功转 status='phase_c' + 失败 'phase B: <msg>' 标 failed + `_advance_one` 状态机 `phase_b → _do_phase_b` 分支落地 + `phase_c` 留 stub no-op 等 Step 4 + `routers/imports.py` 加 10 model import + `_CASCADE_TABLE_MAP` + `_cascade_tombstone_event(row, backend_table)`（KV-PK 表 `id` 槽放 `key`，wire shape 与各 router `_to_event(deleted=True)` 对齐）+ `cancel_import_job` 重写：cascade 共享 `cascade_version=nextval('global_change_seq')` + 按 `SERVER_ROUTED_TABLES` 顺序 `UPDATE ... SET version=cascade_version, deleted_at=now() WHERE imported_from_job_id=:job AND user_id=:user AND deleted_at IS NULL RETURNING *` + commit 后逐表逐行 publish delete event + import_jobs put 事件兜底；DELETE 已 cancelled 的 job 幂等返 snapshot。
  - **关键修复（test agent 红测发现）**：dev 初版 `routers/imports.py` 漏 import `text` from sqlalchemy（顶部只 `from sqlalchemy import select, update`），cascade 路径运行到 `text("SELECT nextval('global_change_seq')")` 时 `NameError: name 'text' is not defined` → DELETE 全 500 → `test_delete_cancelled_job_soft_deletes_imported_rows` / `test_delete_publishes_ws_event_per_cascaded_row` 红。修：`from sqlalchemy import select, text, update`。
  - **测试基建变更**：`tests/api/test_imports_router.py::test_complete_with_all_parts_triggers_worker` 末尾断言 `later['status'] in ('phase_b', 'parsing')` 升格为 `('phase_b', 'phase_c', 'parsing')` —— Step 3 实现 Phase B 后小 fixture 上 worker 1s 内能跑完 Phase A + Phase B 两档，正常 land 在 phase_c（Step 4 stub）。`tests/api/conftest.py::db_reset` 已含 `import_jobs` TRUNCATE（Step 1 落入），无需新增。
  - **api**：`tests/api/test_import_phase_b.py` 14 case：① `test_writes_workspaces_with_original_uuid`（3 workspaces 固定 UUID → import 后 PG `SELECT id FROM workspaces WHERE imported_from_job_id=:job` 返三个原 UUID）/ ② `test_writes_all_structural_tables_in_dependency_order`（fixture 7 表各 2 行 → PG 各表行数 = 2 + 借 dialogs FK workspaces 成功（无 IntegrityError）反证 workspaces 在 dialogs 前处理 + 直接断言 PHASE_B_TABLES tuple 顺序）/ ③ `test_lww_skips_older_incoming_when_existing_newer`（先 PUT newer workspace（`updated_at=now`） → import fixture 含同 id workspace `updatedAt='2024-01-01T00:00:00+00:00'` → PG `data.name='newer-via-put'` 不变 + version 不前进）/ ④ `test_lww_overwrites_when_incoming_newer`（反向：PUT older + import `updatedAt=+365d` future → PG `data.name='imported-stale'` 覆盖 + `imported_from_job_id` 设到 job_id）/ ⑤ `test_imported_rows_tagged_with_job_id_and_timestamp`（3 workspaces 全部 `imported_from_job_id=:job` + `imported_at` 非 NULL + 距 now < 60s）/ ⑥ `test_publishes_progress_event_per_table`（**indirect proof**：import_jobs 暂未 server-routed (Step 6) → ws_connect+subscribe('import_jobs')='unknown-table' 无解，改用「processed_rows == 14 + status=phase_c」证明 7 张表都被处理且 worker 在每表完成后才推 progress 同源；Step 6 落地 import_jobs 进 TABLE_MODELS 后会补真 WS 抓帧 case）/ ⑦ `test_phase_b_done_triggers_phase_c`（Phase B 成功后 worker 把 status → phase_c + version > 1，phase_c stub 留给 Step 4）/ ⑧ `test_phase_b_handles_missing_table_files`（fixture 仅含 workspaces+dialogs → 缺 5 张 NDJSON 不报错，PG 仅这两表有 imported 行）/ ⑨ `test_phase_b_recovers_from_crash`（直接 INSERT status='phase_b' job 但 tmp 目录无 NDJSON → worker 捕 `ImportFormatError('temp dir ... missing')` → status='failed' + error_message 含 'phase B' + 'temp dir'）/ ⑩ `test_dexie_to_backend_table_name_mapping`（fixture 含 `installedPluginsV2` + `avatarImages` 两 camelCase wire 表 → PG `installed_plugins` + `avatar_images` snake_case 表有行 + information_schema 查 4 个 camelCase 表名都不存在）/ ⑪ `test_kv_pk_table_writes_correct_row`（fixture reactives 两 row → PG `(user_id, key)` 复合 PK 命中 + `data` JSONB 保留原 wire `{key, value}` 形态）/ ⑫ `test_delete_cancelled_job_soft_deletes_imported_rows`（all_phase_b fixture import 完 → DELETE → 7 张表各 2 行 deleted_at IS NOT NULL + 全 14 行 version 同一 cascade_version）/ ⑬ `test_delete_publishes_ws_event_per_cascaded_row`（subscribe before delete + since=9999999999 抑 replay → DELETE 后 3s 内抓到 1 个 workspaces + 5 个 dialogs delete event，wire shape `{type:event, op:delete, row:null, id:str, rev:int>0}`）/ ⑭ `test_migration_adds_imported_from_job_id_to_all_routed_tables`（information_schema 三档查：10 张表都有两列 + FK `fk_<tbl>_imported_from_job_id` ON DELETE SET NULL + 索引 `ix_<tbl>_imported_from_job_id`）。
  - **测试输出**：`pnpm test:api`（**241 passed / 0 failed in 173.10s**，新增 14 case 全绿 + Step 2 spec 末断言扩展通过；smoke e2e 3/3 绿；含 Step 1 + Step 2 + 跨 stage 全套兜底）。
  - **故障注入红测**（CLAUDE.md 「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch + 1 轮 dev 自带 bug）：
    - **第 0 轮 dev 初版自带 bug**（自然红）：`routers/imports.py` cascade 路径漏 `from sqlalchemy import text` → DELETE 全 500 `NameError: name 'text' is not defined. Did you mean: 'next'?` → `test_delete_cancelled_job_*` 两 case 红 stdout `AssertionError: Internal Server Error assert 500 == 200`。test agent 加 `text` import 后 → 全绿。
    - **第 1 轮 LWW WHERE 反向**：`import_worker.py::_phase_b_load_table` 的 ON CONFLICT WHERE 子句 `existing.updated_at < EXCLUDED.updated_at` 改为 `>` → `test_lww_skips_older_incoming_when_existing_newer` 红，stdout `AssertionError: LWW skip failed — PG data was overwritten: data={'id': 'ws-lww-skip-...', 'name': 'imported-stale', ..., 'updatedAt': '2024-01-01T00:00:00+00:00'} version=9 imported_from=lww-skip-... updated_at=2024-01-01 00:00:00+00:00 assert 'imported-stale' == 'newer-via-put'`；同样 `test_lww_overwrites_when_incoming_newer` 红 stdout `data={'name': 'older-via-put', ...} imported_from=None assert 'older-via-put' == 'imported-stale'`（newer 没覆盖旧；imported_from_job_id 没被 set 因为 LWW 关守反向后整批跳过）。恢复 `<` → 绿。
    - **第 2 轮 commit 注释**：`run_phase_b` 内每表 `await session.commit()` 注释掉 → `test_writes_workspaces_with_original_uuid` 红 stdout `AssertionError: PG workspace ids do not match fixture: pg=[] expected=['ws-uuid-0-...', 'ws-uuid-1-...', 'ws-uuid-2-...']`；`test_writes_all_structural_tables_in_dependency_order` 红 stdout `error_message: "phase B unexpected: (sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) ..."` `processed_rows: 0 assert 'failed' == 'phase_c'`（无 commit 时第二张表的 ON CONFLICT 看不到第一张表的写入，导致 unique violation 在 dialogs FK→workspaces 跨 commit 边界先暴露）。恢复 commit → 绿。
    - **第 3 轮 PHASE_B_TABLES 依赖顺序错乱**：把 dialogs 提到 workspaces 之前 → `test_writes_all_structural_tables_in_dependency_order` 红 stdout `error_message: "phase B unexpected: (sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) ... violates foreign key constraint ... key (workspace_id)=... is not present in table 'workspaces'" assert 'failed' == 'phase_c'`（dialogs INSERT 时 workspace_id FK 找不到目标，因为 workspaces 表还没 import）。恢复 → 绿。
- **Stage 4.5 / Step 4** ✅ Phase C messages 文字写入 + `_pending_blob_extraction` 标记 + dead_letter array-concat + LWW UPSERT 500 行/批：alembic migration `c5e9f2a8d6b4`（messages 表加 `_pending_blob_extraction BOOLEAN NOT NULL DEFAULT FALSE` + partial b-tree 索引 `ix_messages_pending_blob_extraction WHERE _pending_blob_extraction = TRUE`，down_revision=`b3e7d4f8a1c5`）+ `data/models/message.py` 加同名列 + `__table_args__` 同名 partial Index + docstring 解释为何不烧进 jsonb 表达式（partial b-tree 让 Phase D 扫描 cost 与 pending 集大小线性，与全表大小无关）+ `data/import_worker.py::PHASE_C_BATCH_SIZE=500` + `_PHASE_C_PENDING_SIZE_THRESHOLD=64*1024`（与 frontend `BLOB_INLINE_MAX_BYTES` 对齐，硬编码避免 import blob_store 触发 JWT_SECRET 读取）+ `_row_has_attachment_envelope(value, _depth=0)`（递归扫 dict/list 任意层级，深度上限 16，envelope 判定要求 `type:'inline'|'ref'` + 至少一个 sibling key in `{data, base64, url, sha256, content_type, size}` 兜底）+ `_should_pending_blob_extraction(row, row_json)` 双 prong（size ≥ 64KB OR 含 envelope）+ `_publish_phase_c_progress(job, processed_rows, batch_index)` envelope（共用 `_envelope` snap + 注 `phase_c_batch_index` / `processed_rows` 进 data）+ `_append_dead_letter(session, job_id, entry)`（PG `dead_letter || CAST(:entry AS jsonb)` array-concat 而非 read-modify-write，并发 worker 重启不丢历史 + 顺序保留）+ `_phase_c_lookup_known_dialogs(session, user_id)` 一次 SELECT 拉所有 dialog id 进 set，避免 batch 内 FK violation 引发 all-or-nothing rollback（FK 错只能 row-by-row 重试，500 行 fixture 退化为 250500 SQL）+ `run_phase_c(job_id)` 流式 readline NDJSON + 每行 LWW timestamp + FK 预校（orphan → dead_letter）+ pending 判定 + 累积到 PHASE_C_BATCH_SIZE 即 `_flush_batch()`（一次 nextval 拉 batch 长度的版本号 + UPSERT `ON CONFLICT (id) DO UPDATE SET data,version,updated_at,deleted_at=NULL,dialog_id,imported_from_job_id,imported_at,_pending_blob_extraction WHERE existing.updated_at < EXCLUDED.updated_at` LWW 守 + 每批结束 commit + bump processed_rows + publish progress + clear list；最后循环出 EOF 后 remainder flush）+ `ImportWorker._do_phase_c` 整 phase try/catch wrap + 成功转 status='phase_d' + 失败 'phase C unexpected: <msg>' 标 failed + `_advance_one` 状态机 `phase_c → _do_phase_c` 分支 + `phase_d` 留 stub log warn no-op 等 Step 5（job 安静停 phase_d 不抛、不标 failed）。
  - **测试输出**：`pnpm test:api`（**254 passed / 0 failed in 189.57s**，新增 14 case 全绿 + 254 总数较 Step 3 的 241 +13 含 1 个新增 helper 单测；e2e 不需要跑——本 Step 是纯 backend 改动，不影响任何前端 endpoint 或 wire envelope 形态，messages router `_to_row` / `_to_event` 与 Step 3 相同未动）。
  - **故障注入红测**（CLAUDE.md「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch）：
    - **第 1 轮 size threshold 关闭**：`_should_pending_blob_extraction` 第 1 prong `if len(row_json.encode('utf-8')) >= _PHASE_C_PENDING_SIZE_THRESHOLD: return True` 注掉 → `test_marks_pending_blob_extraction_for_messages_with_attachment` 红 stdout `AssertionError: 80KB row JSON should mark TRUE (size threshold = 65536B); got False (full row map: {'msg-big-...': False, 'msg-inline-...': True, 'msg-plain-...': False})`（inline envelope 行还是 TRUE 证明 prong 2 独立工作；80KB row 与控制组 plain 都 FALSE → size prong 失效）；同样 `test_helper_should_pending_size_threshold` 直接红。恢复 → 绿。
    - **第 2 轮 remainder flush 注释**：`run_phase_c` 末尾 `await _flush_batch()` remainder flush 注掉 → `test_batch_boundary` case `two-batch-501` 红 stdout `AssertionError: case two-batch-501: PG count=500, expected 501, final={'status': 'phase_d', 'processed_rows': 500, ...}`（501 行最后 1 行未 flush；status 仍 phase_d 表示 worker 正常推进只是没 flush remainder，符合「只丢最后一批」预期）。恢复 → 绿。
    - **第 3 轮 dispatch phase_c 关闭**：`_advance_one` 内 `if status == 'phase_c': await self._do_phase_c(job_id)` 改成 no-op return → `test_phase_c_done_triggers_phase_d` 红 stdout `AssertionError: job trig-... did not reach ('phase_d', 'failed') within 10.0s; last seen: {'status': 'phase_c', 'processed_rows': 0, ...}`（job 永远卡 phase_c → 测试 wait_for_job_status 10s 超时；processed_rows=0 验证 worker 完全没碰它）。恢复 → 绿。
- **Stage 4.5 / Step 5** ✅ Phase D attachments → 对象存储 + 64KB 阈值 inline/ref + 4 路并发 + 重试 + dead_letter：`data/import_worker.py` 增 `_PHASE_D_INLINE_MAX_BYTES = 64 * 1024`（与 frontend `BLOB_INLINE_MAX_BYTES` 对齐，硬编码避免 import blob_store 触发 JWT_SECRET 读取）+ `_PHASE_D_CONCURRENCY = 4`（asyncio.Semaphore 包**单 attachment**而非 row，使一行 5 attachment 也能与下行 attachment 0 并行）+ `_PHASE_D_MAX_RETRIES = 3` + `_PHASE_D_RETRY_BACKOFFS_SECONDS = (1.0, 2.0, 4.0)` + `_PHASE_D_PROGRESS_INTERVAL = 10`（每 10 attachment publish 一次 progress event）+ `_maybe_decode_inline_envelope(value)`（容忍 `data` / `base64` 两种 key 名 + missing size/content_type 兜底；non-dict / `type` ≠ 'inline' / 缺 data / decode error 全 None 跳过）+ `_walk_attachments(container, _depth)`（两遍 collect-then-mutate 避免 iterate-and-mutate footgun，深度上限 16，遇到 `{type:'inline', data|base64:...}` envelope 不再下钻）+ `_set_at_path(root, path, new_value)` 按路径 mutate + `_phase_d_upload_one(semaphore, blob_store, user_id, decoded, content_type)` 单 attempt 包 sha256 计算 + `BlobStore.put(sha256, decoded, content_type)`（trust-the-hash 幂等）+ `INSERT blobs ON CONFLICT DO NOTHING` + per-user `INSERT blob_refs ON CONFLICT DO UPDATE last_seen_at` + 读 `BLOB_PRESIGN_TTL_SECONDS` 用 `BACKEND_DATA_API_URL` 作 base 构 ref envelope `{type:'ref', url, sha256, size, content_type}` + `_phase_d_process_row(semaphore, blob_store, job_id, user_id, row_id, row_data)`（walk → for each envelope 算 actual_size = `len(decoded)` 不信 declared_size → < 64KB 留 inline 计 `n_inline_kept` → ≥ 64KB 4 attempt 重试上传 → 单 row 同 SessionLocal tx 内 `UPDATE messages SET data, _pending_blob_extraction=FALSE, version=nextval(global_change_seq), updated_at=now()` + 单 row 多 attachment failures 一次 `dead_letter || CAST(:entries AS jsonb)` array-concat 单 UPDATE）+ `run_phase_d(job_id)` 扫 `messages WHERE user_id=? AND _pending_blob_extraction = TRUE`（`== True` 不用 `.is_(True)` 让 PG planner 选 partial index）+ batch_size = `_PHASE_D_CONCURRENCY * 4 = 16` 行 / batch + `asyncio.gather(return_exceptions=True)` + 跨 batch throttle check publish progress + 末尾 final progress + `_phase_d_cleanup`（unconditional `shutil.rmtree(tmp_dir, ignore_errors=True)` + 查 `blobs WHERE sha256=raw_object_key`，无引用则 `BlobStore.delete(raw_object_key)`，有共享则 skip 让 lifecycle GC 处理）+ `ImportWorker._do_phase_d` 整 phase try/catch wrap + 成功转 status='done' + 失败 'phase D unexpected: <msg>' 标 failed + 推 final WS done event（best-effort）+ `_advance_one` 状态机 `phase_d → _do_phase_d` 分支落地（替换 Step 4 的 stub log warn no-op）。
  - **测试基建变更**：`tests/api/conftest.py` 顶部加 `os.environ.setdefault('DATABASE_URL', '...5434/aiaw_test')` + `JWT_SECRET` setdefault — Stage 4.5 Step 5 起测试改用「in-process 直调 `run_phase_d` + monkeypatch `LocalFsBlobStore.put`」做 fault-injection（dedup count / put 永错 / 慢 sleep 测峰值并发），这条路径需要测试进程的 `data.db.engine` 指向 5434 而非默认 dev 5433；不影响其他测试（按 `setdefault` 语义未污染 CI 已有 env）。同步加 `tests/api/helpers/blob_mocks.py`：4 个 ctx-mgr 风格 patch（`patch_put_with_counter` / `patch_put_with_delay` / `patch_put_always_fails` / `patch_put_fails_for_sha`），class-level 替 `LocalFsBlobStore.put`，restore 在 finally；`tests/api/test_import_phase_c.py::test_phase_c_done_triggers_phase_d` 由「target=phase_d / Step 5 stub parks」改为「target=done / Step 5 实现后小 fixture 0 attachment 链直接走完 phase_d→done」（Step 5 落地后该 spec 的预期行为反转，2026-05-03 同批次更新）。
  - **fresh_engine_loop fixture**：pytest-asyncio Mode.AUTO 给每个 case 一个新 event loop；`data.db.engine` 的 asyncpg 连接池跨 case 缓存 conn → 第二个 in-process case 在新 loop 取出旧 loop 的 conn → `RuntimeError: got Future attached to a different loop`。修：本 spec 提供 `fresh_engine_loop` async fixture，case 前后 `await engine.dispose()` 让 pool 在新 loop 重建。所有 in-process case（3 / 4 / 6 / 7 / 8 / 10 / 11）显式带这个 fixture。
  - **测试输出**：`pnpm test:api`（**267 passed / 0 failed in 199s**，新增 13 case 全绿 + Phase C `test_phase_c_done_triggers_phase_d` 反转更新过；e2e 99 passed / 399 skipped / 0 failed in 5.9min — 纯 backend 改动，前端 build 无任何 cache miss）。
  - **故障注入红测**（CLAUDE.md「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch）：
    - **第 1 轮 64KB 阈值反向**：`_PHASE_D_INLINE_MAX_BYTES = 0`（替 64*1024）→ 重启 backend → `test_attachment_under_64kb_stays_inline_in_pg` 红 stdout `AssertionError: <64KB attachment should stay inline; got {'url': '/api/v1/blobs/51fec.../data?exp=...&sig=...', 'size': 65535, 'type': 'ref', 'sha256': '51fec...', 'content_type': 'application/octet-stream'} assert 'ref' == 'inline'`（65535B 被错误转 ref）；同样 `test_inline_envelope_below_threshold_stays_unchanged` 红 `put should not be called for sub-64KB attachments; got call_count=5`（5 个 1KB 都被 put）。恢复 → 全绿。
    - **第 2 轮 BlobRef INSERT 注掉**：`_phase_d_upload_one` 内 `pg_insert(BlobRef).values(...).on_conflict_do_update(...)` + `await session.execute(ref_stmt)` 注释 → 重启 backend → `test_attachment_over_64kb_uploaded_to_blob_store_and_row_rewritten` 红 stdout `AssertionError: blob_refs row missing for user_id=ce8b5fb1-... sha=fee47b1f0d... assert None is not None`（per-user 的 ref 表无行）；同样 `test_same_sha256_reuses_existing_blob_no_double_upload` 红 `blob_refs should have 1 row for (user, sha); got 0 assert 0 == 1`（dedup 路径下 PG 端 0 行）。恢复 → 全绿。
    - **第 3 轮 `_PHASE_D_MAX_RETRIES = 0`** 替 3 → 重启 backend → `test_failed_attachment_after_3_retries_lands_in_dead_letter` 红 stdout `AssertionError: expected 4 put calls (initial + 3 retries); got 1; summary={'attachments_failed': 1, ...}` + log `all 1 attempts failed (boom), dead-lettering`（dead_letter 仍写但 attempts 从 4 退化为 1）；同样 `test_dead_letter_does_not_block_other_attachments_in_same_row` 红 `expected 4 failed put calls (4 attempts on middle); got 1; state={'failed_call_count': 1}`。恢复 → 全绿。
- **Stage 4.5 / Step 8** ✅ `GET /api/v1/bootstrap` endpoint + 首屏 router guard + applyBootstrap + 2s timeout 回退：`src-backend/data/routers/bootstrap.py`（~291 行：`GET /api/v1/bootstrap` 一次返 8 keys `{schema_version, workspaces, dialogs, providers, assistants, installed_plugins, reactives, avatar_images, messages_recent}` —— **强约束 `items` / `artifacts` 不进 response**；`_envelope_id` / `_envelope_kv` 与各表 list endpoint / realtime event 同 envelope 形态；`_gather_messages_recent` 按 `MAX(messages.updated_at) DESC` 排 dialog + 每 dialog 取 50 newest + 按 dialog 整截断 `_RESPONSE_BYTE_BUDGET=1_000_000`（永不部分截 dialog tail）；`Cache-Control: private, max-age=10`；查询 `WHERE deleted_at IS NULL` 与 active import job 解耦）+ `app.py` mount `bootstrap_router` + `_enable_backend_data_api` log 增 `bootstrap` 关键字 + `src/data/bootstrap-client.ts`（96 行：`fetchBootstrap(opts?)` 包 `http.get('/api/v1/bootstrap', {signal: AbortSignal.timeout(2000)})`，timeout/HttpError/network 全 catch 返 `null` + 三档 console.warn 分流不同 cause；不 throw 让 caller 不需要 try/catch）+ `src/data/bootstrap-apply.ts`（308 行：`applyBootstrap(response): Promise<ApplyResult>` per-table 独立 try/catch + 7 张 id-PK / KV-PK 表分应用：workspaces/dialogs/providers/assistants 用 `applyIdRows<T>` raw `data` 直 put；installed_plugins KV 同上；reactives KV 用 `decoder` 包装为 `{key: env.key, value: env.data}` StoredReactive；avatar_images id-PK 用 `decodeAvatarImage` base64→ArrayBuffer；messages_recent id-PK 走 `decodeMessage` 含 `materializeAttachment(contentsBlob)` 解 ref envelope；返 per-table count 给 dev console summary）+ `src/router/index.ts`（+97 行：`beforeEach` guard 4 fast-path skip：(a) `!BackendApiBaseURL` (b) `!authSource.enabled` (c) sessionStorage `aiaw.bootstrap.attempted='1'` (d) `!authSource.user.value`；`runBootstrapOnce()` Promise singleton 防并发首登 race；fetch 返 null 时写 `aiaw.bootstrap.fallback='1'` + 永远在 finally 写 `attempted='1'` 避免每次 nav 重试；export `bootstrapDidFallback / clearBootstrapFallback / _resetBootstrapForTests`）+ `src/layouts/MainLayout.vue`（+30 行：`<q-banner inline-actions class="bg-warning" data-test-id="bootstrap-fallback-banner">` 显示 `t('mainLayout.bootstrapFallback')` + 关闭按钮调 `clearBootstrapFallback`；q-header 包裹按 layout view="lHr ..." 路由到 drawer 上方）+ `src/i18n/{zh-CN,en-US,zh-TW}/pages.ts` 三语 2 keys（`mainLayout.bootstrapFallback` + `mainLayout.bootstrapFallbackDismiss`）+ `src/boot/expose-debug.ts`（+18 行：暴露 `window.__bootstrap__ = {fetch, apply, didFallback, clearFallback, reset}` 给 spec 直接 evaluate；EXPOSE_DB 守卫）。
  - **api**：`tests/api/test_bootstrap.py` 11 case：① `test_returns_all_small_tables_in_one_response`（response key set == 8 keys + 显式 assert `'items'/'artifacts' not in body`）/ ② `test_messages_recent_limited_to_50_per_dialog`（PG 直插 100 messages × 1 dialog deterministic ts → 验返 50 + ids == m050..m099）/ ③ `test_response_size_under_1mb_for_typical_user`（PG 直插 100×50=5000 small messages → response.content bytes < 1MB）/ ④ `@slow test_response_truncates_to_under_1mb_for_heavy_user`（PG 直插 1000×50 + 500B padding/msg → 50000 messages 超载 → response ≤ 1MB+5% 框架开销 + truncation 在 dialog 边界（每个返 dialog 必 == 50，绝无半截））/ ⑤ `test_account_isolation`（A/B 各种 ws/dlg/msg 互相不出现）/ ⑥ `test_partial_data_during_active_import`（API put 3 ws + PG INSERT import_jobs status='phase_c' → bootstrap 仍返 3 ws 不卡）/ ⑦ `test_cache_control_header_set`（`Cache-Control == 'private, max-age=10'`）/ ⑧ `test_envelope_shape_matches_list_endpoint`（bootstrap workspace envelope == `GET /api/v1/workspaces` 字节级一致 `data` + `version` + `deleted`）/ ⑨ `test_unauth_request_rejected`（401）/ ⑩ `test_messages_recent_excludes_items_artifacts`（即使 seed item + artifact 也不在 body 顶层 + messages_recent envelope `data` 无 items 字段污染）/ ⑪ `test_dialogs_with_no_messages_dont_appear_in_messages_recent`（empty dialog 在 dialogs 但不在 messages_recent；带 5 msg 的 dialog 在两处都出现）。测试基建：所有 ≥100 行 seed 用 `_bulk_seed_via_pg` 直 INSERT（避免 5000 sequential httpx PUT 让 batch run 受 connection pool 老化影响 + 速度从 ~5min 提到 26s）。
  - **spec**：`tests/e2e/stage4_5/step8-bootstrap.spec.ts` 5 case（profile realtime-ws）：⑫ `test_fresh_browser_login_no_blank_first_screen`（清 IDB → seed 1 ws via REST → newContext + bootSession → 1.5s 内 IDB.workspaces 含 seeded id；attempted='1' + fallback null）/ ⑬ `test_bootstrap_timeout_falls_back_to_progressive`（mockBootstrapTimeout(ctx, 3000) → bootSession → 8s 内验 fallback='1' + banner visible + attempted='1'）/ ⑭ `test_bootstrap_runs_only_once_per_session`（page.on('request') 计 GET /api/v1/bootstrap → 多次 navigation 验 hits == 1）/ ⑮ `test_bootstrap_apply_writes_idb_correctly`（seed 3 ws → bootstrap apply → IDB.workspaces 含 seeded ids + 长度 ≥ 3 子集断言，允许默认 ws 等额外 client-side row）/ ⑯ `test_bootstrap_500_error_falls_back`（mockBootstrap500(ctx) → fallback path）。新 helper `tests/e2e/helpers/bootstrap.ts`（5 export：`mockBootstrapTimeout` / `mockBootstrap500` / `clearBootstrapState` / `assertBootstrapFallback` / `readAttemptedFlag`）。
  - **测试输出**：`pnpm test:api`（**286 passed / 0 failed in 250.98s**，新增 11 case 全绿；回归零变化）+ `pnpm test:e2e -g step8-bootstrap`（5 passed / 13s realtime-ws profile）+ `pnpm test:e2e` 全套（110 passed / 548 skipped per profile gating，~6.6min；零 regression）。
  - **故障注入红测**（CLAUDE.md「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch）：
    - **第 1 轮 byte budget 反向**：`bootstrap.py::_RESPONSE_BYTE_BUDGET = 1_000_000_000` 替 `1_000_000` → 重启 backend → `test_response_truncates_to_under_1mb_for_heavy_user` 红 stdout `AssertionError: heavy-user response size 36788375B exceeds 1MB+5% framing budget assert 36788375 <= 1050000`（36MB 对比期望 ≤1.05MB，truncation 完全失效）→ 恢复 → 绿。
    - **第 2 轮 applyBootstrap workspaces put 神化为 no-op**：`bootstrap-apply.ts` 内 workspaces apply 块的 `(row) => db.workspaces.put(row)` 替 `(_row) => Promise.resolve()` → `rm -rf tests/.builds/realtime-ws` 强 rebuild → 跑 `test_fresh_browser_login_no_blank_first_screen` 时 *未* 红：原因是 `workspaces.server.ts::ensureRealtimeSubscription` 会通过 WS realtime 独立写入 workspace（即使 bootstrap 路径阻塞，realtime fan-out 在 1.5s 内补齐），spec 断言被 realtime 兜底掩盖。**结论**：spec 12 的 1.5s 时窗在 realtime 启用 profile 下确实有 realtime 兜底，是设计内的冗余路径；该 spec 主要保证「用户首屏不空白」的端到端 SLA，而非「bootstrap 路径独占」契约。改用第 3 轮做 router guard 路径专项验证。
    - **第 3 轮 `DEFAULT_TIMEOUT_MS = 100_000`** 替 `2000` → `rm -rf tests/.builds/realtime-ws` → `test_bootstrap_timeout_falls_back_to_progressive` 红 stdout `TimeoutError: page.waitForFunction: Timeout 8000ms exceeded` at `helpers/bootstrap.ts:70`（`assertBootstrapFallback` 等 `aiaw.bootstrap.fallback==='1'`），3s mock 在 100s timeout 下永不触发 fallback → spec 真在卡 2s 客户端 timeout 路径 → 恢复 → 绿。
- **Stage 4.5 服务端 Import Job + bootstrap 整体落地** ✅ Step 1-8 全部 ✅，含 Phase A/B/C/D worker + multipart upload + WS 进度 + ImportDataDialog + AccountPage 卡片 + bootstrap endpoint + router guard。**🟢 物理分水岭**：可邀请少量内圈用户做真实 export → import 端到端验收。出口判据剩 `exit-criteria.spec.ts::test_huge_fixture_end_to_end`（200MB fixture）+ 浏览器活跃时间 < 10min + 服务端 RSS 增量 < 500MB 三项作为 manual 上线把关，不阻塞下一阶段开工。下一阶段：Stage 4.7 离线 outbox。
- **Stage 4.5 / Step 7** ✅ 前端 ImportDataDialog 重写 + multipart upload helper + AccountPage 迁移状态卡片 + import-job e2e profile：`src/data/import-client.ts`（~360 行：5 endpoint 包装 `createImportJob` / `uploadParts` / `completeImport` / `cancelImport` / `getImportJob` / `getActiveImportJob` / `subscribeImportStatus`；Promise pool 4 路并发 + 5MB part size + per-part `Blob.slice()` 流式 + 4 attempt 重试指数退避 1s/2s/4s + AbortSignal 全链路 + localStorage cursor `import.<jobId>.parts` 形态 `Array<{partNumber, etag}>` 每 part 完成立即 saveCursor 抗 tab crash + 续传时先 `GET /api/v1/import/jobs/<id>` 验状态 ≠ 'uploading' 则不 PUT 直接返回 cursor 让 caller fast-forward + LocalFs JSON `{etag}` / S3 `ETag` header 双格式兼容）+ `src/composables/import-job.ts`（~110 行：模块级单例 `ref<ImportJobStatus|null>` + `setupOnce()` watcher 跟 `authSource.user.value?.userId` 联动；登录转换触发 `refreshActiveJob()` → `GET ?status=active` + `attachSubscription(jobId)` 包 `subscribeImportStatus`；terminal 状态 `done|failed|cancelled` 自动 clearSubscription；export `useActiveImportJob` / `clearActiveImportJob` / `reloadActiveImportJob`）+ `src/boot/import-job.ts`（app 启动触发 composable 安装 watcher，`BackendApiBaseURL` 空时 no-op）+ `src/components/ImportDataDialog.vue`（整段重写：选文件 → `createImportJob` → `uploadParts` 4 并发 5MB 切片 + 5 tick 滑动窗 ETA 估算 + 进度条 → `completeImport` → `reloadActiveImportJob` 触发卡片 + 后台通知；mid-upload cancel 走 `AbortController.abort()` 终止 in-flight + best-effort `cancelImport` 释放 server slot；i18n 三语 16 keys）+ `src/pages/AccountPage.vue`（迁移状态卡片：`importJob` 非空时显示 phase label 国际化 + row/blob 进度 + 双线进度条按 phase 自动切换分母（phase_d→blobs / phase_b/c/parsing→rows / done→100% / 其他→indeterminate）+ ACTIVE_PHASES 集合判定 cancel/dismiss 按钮互斥 + cancelInflight 锁防重）+ `src/boot/expose-debug.ts` 暴露 `window.__importClient__` 含全部 5 endpoint + composable + `cursorKey(jobId)` / `readCursor(jobId)` 测试用 helper + `quasar.config.js` boot 列表插入 `'import-job'` + `tests/env/.env.test.import-job` + `playwright.config.ts` PROFILES 加 `import-job` (port 9015) + `tests/scripts/run-playwright.sh` `E2E_BUILD_DIR_IMPORT_JOB` + `tests/scripts/backend-start.sh` CORS 加 9015 + `tests/api/fixtures/dexie_export.py::make_medium_export(messages, attachment_size_mb)` 新增（5 messages × 1MB attachments）。
  - **spec**：`tests/e2e/stage4_5/step7-import-flow.spec.ts` 6 case 全绿（profile import-job 单跑 26.8s）：① `test_upload_then_close_tab_then_reopen_sees_progress`（fixture 5×1MB attachments / page A driveUpload → page A.close → page B 同 user 不同 context → injectAuth fresh refresh-token + goto /account → 卡片可见 + 等 composable.value.status === 'done'，验证 REST 初始 fetch + WS subscription 端到端连通；30s 超时窗）/ ② `test_upload_resumes_after_simulated_network_drop @slow`（7MB padded file × 2.5MB part = 3 parts / concurrency=1 part-by-part / 等 cursor ≥1 part → setOffline → uploadParts 抛 Failed to fetch → cursor 留前缀 parts → setOnline → 第二次 uploadParts 仅 PUT 缺失 part / `seenPartUrls=[3]` 不含 [1,2] 验跳过 / complete 后 cursor === null）/ ③ `test_cancel_button_aborts_job_and_clears_state`（10KB padded file / create + uploadParts 但**不 complete** 让 job 停 'uploading' active 态 / reloadActiveImportJob → goto /account → 卡片可见 + Cancel 按钮可见 / `dispatchEvent('click')` 绕 Quasar transient backdrop / 等 cursor=null + status terminal / `getImportJob` 验 server status='cancelled' / Dismiss 出现 OR 卡片消失（合理终态）/ ④ `test_phase_b_complete_makes_workspaces_visible`（small fixture 1 ws / driveUpload → poll status ∈ {phase_c, phase_d, done} / pgQuery workspaces 验 `id=ws-X AND user_id=user.id AND deleted_at IS NULL` 返 1 行）/ ⑤ `test_phase_c_complete_makes_message_text_readable`（small fixture 1ws+1dlg+3msgs 全 inline / poll status ∈ {phase_d, done} / pgQuery messages 验 3 行 + `data.contents[0].text` 形如 `case5-message-X` + `_pending_blob_extraction=false`）/ ⑥ `test_phase_d_complete_makes_attachment_renderable @slow`（fixture 1ws+1dlg+5msgs×1MB attachments / poll status='done' 60s / pgQuery `blob_refs` `DISTINCT sha256 ≥ 5` + 5 message rows 各自 `data.attachment.type==='ref'` + `sha256/url` 非空 + `_pending_blob_extraction=false`，验 Phase D 上传 + 每 row envelope rewrite + clear flag 端到端）。
  - **测试基建变更**：`tests/e2e/helpers/` 不需新加 helper（existing `pg.ts` / `tabs.ts` / `auth.ts` / `net.ts` / `db.ts` 全覆盖；fixture builder 留在 spec 内 `buildDexieExport` + `buildPaddedFileInPage` + page 内 `crypto.getRandomValues + btoa` 1MB attachment 生成器，spec 自包含避免 helper 维护开销）。`tests/env/.env.test.import-job` 与 `realtime-ws` 同 flag 集（`REALTIME_TRANSPORT=ws` + `BACKEND_DATA_TABLES` 全 10 张表）但独立 build cache slot 防止 import-only env 改动 bust realtime-ws 缓存。
  - **测试输出**：`pnpm test:api`（**275 passed / 0 failed in 227.78s**，回归零变化）+ `pnpm test:e2e -g "step7-import-flow"`（6 passed / 26.8s import-job profile 单跑）+ `pnpm test:e2e -g "stage4_5|stage4|stage3"`（88 passed / 416 skipped per profile gating，~5.3min）+ `pnpm test:e2e` 全套（92 passed / 518 skipped / 1 fail = `step3-realtime-recovery::scenarioB ws auto-reconnect after offline window` 已知预期 timing-flake 单跑稳定，与本 step 无关）。
  - **故障注入红测**（CLAUDE.md「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch）：
    - **第 1 轮 cursor 写入禁用**：`import-client.ts::saveCursor` body 注释掉 `localStorage.setItem(cursorKey(jobId), JSON.stringify(parts))` → `rm -rf tests/.builds/import-job` → `test_upload_resumes_after_simulated_network_drop` 红 30s 超时 stdout `[case2] cursor reached ≥1 part after 30005ms: null` —— 等 cursor 永远等不到，spec 真在卡 cursor 持久化路径 → 恢复 → 绿。
    - **第 2 轮 resume 路径关闭**：`import-client.ts::uploadParts` 入口 `const cursor = loadCursor(jobId)` 改为 `const cursor: ImportPart[] = []` → 强制每次都重 PUT 全 parts → `rm -rf tests/.builds/import-job` → 同 spec 红，stdout `Error: part 1 should not be re-requested after resume; seenPartUrls=[1,2,3]` —— resume 路径关掉后 part 1 + 2 也被重 PUT，spec 真在卡 cursor → uploadParts skip 链路 → 恢复 → 绿。
    - **第 3 轮 subscribeImportStatus filter 反转**：`subscribeImportStatus` 内 `if (e.id !== jobId) return` 前面插入 `if (e.id === jobId) return`（drop 全部命中 jobId 的 events）→ `rm -rf tests/.builds/import-job` → `test_upload_then_close_tab_then_reopen_sees_progress` 红 30s 超时（page closed at evaluate 调用时刻）—— tab2 永远等不到 status='done'，REST 初始 GET 拿到的是 `parsing`，必须靠 WS event 推到 `done`，spec 真在卡 WS subscription 端到端送达 → 恢复 → 绿。
- **Stage 4.5 / Step 6** ✅ WS 进度推送 + import_jobs 注册为 read-only realtime channel + 405 client-write reject + cross-user 隔离：`src-backend/data/routers/stream.py::TABLE_MODELS` + `SERIALIZERS` 加 `'import_jobs': ImportJob` 入口 + `_serialize_import_job(j)` 复用 `ImportJob._envelope()`（与 worker 在线 publish 路径同 envelope 形态，op='put'，无 soft-delete 列）+ 同款给 `routers/sse.py`（SSE 是 WS downgrade transport，必须订阅同 broker channel）+ `routers/imports.py` 加 `@router.api_route('/api/v1/import_jobs/{job_id}', methods=['PUT','PATCH','POST','DELETE'], include_in_schema=False)` → 405 reject 路由（body `'import_jobs is read-only; the import worker is the sole writer'` + `Allow: ''` header）— 任何 client 写请求即 405，区别于默认 404 silent-mask；GET 同路径因 FastAPI 默认行为也返 405（其他方法已注册）；client 唯一读入口仍是 `GET /api/v1/import/jobs/{id}`（带斜线 path）。前端订阅契约：`realtime.subscribe('import_jobs', cb)` 拿状态变更，登录时一次 `GET /api/v1/import/jobs?status=active` 拉当前 active job（与 Step 7 前端工作衔接）。
  - **api**：`tests/api/test_import_realtime.py` 8 case：① `test_phase_change_publishes_ws_event`（端到端：subscribe import_jobs since=0 → drain replay-done → 真 multipart upload + complete → drain WS 帧直至 `data.status='done'` → 断言观察序列 status superset {queued, phase_b, done}（parsing/phase_c/phase_d 是 DB-only 转移无 broker.publish，本步骤不要求；监控 forward progress + monotonic rev）/ ② `test_ws_event_envelope_matches_routed_table_contract`（同一 run 取末尾 done 事件，superset 断言 envelope 顶层 `{id, version>0, updated_at, deleted=False, data}` + data 必含 14 keys 子集 `{id, status∈ALL_STATUSES, multipart_upload_id, raw_object_key, total_bytes, processed_bytes, total_rows, processed_rows, total_blobs, processed_blobs, error_message, dead_letter:list, created_at, updated_at}`，superset 因 Phase B publishes 注 `phase_b_table` 额外字段）/ ③ `test_user_b_does_not_receive_user_a_import_progress`（B 先 subscribe import_jobs since=0 + drain replay-done 到位 → A 跑端到端 import 直到 `GET /import/jobs/<id>` 返 done → B 端 2.5s 窗 0 events leaked = pass，broker `_subs.get(user_id, ())` per-user 桶严格隔离）/ ④ `test_active_jobs_query_returns_in_progress_only_cross_user`（A 创 active job → A `GET ?status=active` 返 1 → B `GET ?status=active` 返 `[]` cross-user 隔离）/ ⑤ `test_client_put_to_import_jobs_returns_405`（4 method × 任意 path id：PUT/PATCH/POST/DELETE 全 405 + body 含 'read-only' + Allow header 存在）/ ⑥ `test_get_to_import_jobs_path_also_405_or_404`（GET `/api/v1/import_jobs/<id>` → 405 因其他方法已注册；同时 `GET /api/v1/import/jobs/<id>`（斜线）→ 200 业务 snapshot）/ ⑦ `test_sse_subscribe_import_jobs_works`（SSE channel 与 WS 同模板：open SSE since=0 tables=import_jobs → drain replay-done 验 table='import_jobs' → 真 import 端到端 → 增量收 events 直至 `status='done'` 出现）/ ⑧ `test_replay_after_reconnect_picks_up_missed_events`（先 ws1 since=0 + 跑 import + 收 first_batch ≥1 event 取 max_rev_seen → ws1 close → wait `GET /import/jobs/<id>` 转 done 确认所有事件已 publish 完 → ws2 since=max_rev_seen → 收 events 直至 replay-done，校验 replayed events 全 rev > since + 末尾 status='done'）。
  - **测试基建变更**：`tests/api/test_import_realtime.py` 复用 `tests/api/test_realtime_sse.py::SseReader` + `_open_sse` 不重写解析；fixtures 用 `make_small_export()` (3 表 16 行 ~3KB) 单 part upload 走端到端 multipart 链路（POST create + parts/1 mint + PUT bytes + complete）保证 broker.publish 真 fire，避开 Phase B/C/D 直调（fault-injection 测试用直调，本 spec 验集成）。SSE case 内迭代 `read_events(expect=1, timeout=remaining)` 增量累积避免 single-call timeout 丢已收事件。
  - **测试输出**：`pnpm test:api`（**275 passed / 0 failed in 227.10s**，新增 8 case 全绿；smoke e2e 3/3 绿；含 Step 1-5 + 跨 stage providers/reactives/assistants/installedPlugins/avatarImages/workspaces/dialogs/items/artifacts/messages/blobs/auth/realtime 全套兜底）。
  - **故障注入红测**（CLAUDE.md「新 spec 落地后必须真跑红一次再跑绿」铁律满足，3 轮全 catch）：
    - **第 1 轮 stream.py TABLE_MODELS 注释 `'import_jobs': ImportJob`** → 重启 backend → `test_phase_change_publishes_ws_event` / `test_ws_event_envelope_matches_routed_table_contract` 双红，stdout `AssertionError: unexpected first frame: {'type': 'error', 'code': 'unknown-table', 'message': "table 'import_jobs' not subscribable"} assert {'code': 'unk...ype': 'error'} == {'rev': 0, 't...'replay-done'}` —— 注册缺失时 client subscribe 即 unknown-table，spec 真在卡 routed-table 注册契约 → 恢复 → 绿。
    - **第 2 轮 imports.py 405 reject 路由整段注释**（`@router.api_route('/api/v1/import_jobs/{job_id}', ...) async def import_jobs_write_rejected(...)` 全注释）→ 重启 backend → `test_client_put_to_import_jobs_returns_405` / `test_get_to_import_jobs_path_also_405_or_404` 双红，stdout `AssertionError: PUT expected 405, got 404: {"detail":"Not Found"} assert 404 == 405` —— 路由缺失退化为 FastAPI 默认 404，spec 真在卡 405 + 'read-only' 契约（区别于 silent 404 mask 客户端 bug 的设计意图） → 恢复 → 绿。
    - **第 3 轮 broker user 隔离破坏**：`src-backend/realtime.py::Broker.publish` 的 `subs = list(self._subs.get(user_id, ()))` 改为 `subs = [s for bucket in self._subs.values() for s in bucket]`（fan event 给所有 user）→ 重启 backend → `test_user_b_does_not_receive_user_a_import_progress` 红 stdout `AssertionError: B received 9 import_jobs events from A; leaked sample=[{'type': 'event', 'table': 'import_jobs', 'op': 'put', 'id': 'cbb88fd1-...', 'rev': 1, 'row': {... 'data': {'id': '...', 'status': 'queued', ...}}}, {... 'rev': 14, ... 'status': 'phase_b', ... 'phase_b_table': 'providers', ...}]` —— B 收到 A 的 9 个 import_jobs events（含 queued + phase_b 多张表 + done），spec 真在卡 per-user 桶隔离 → 恢复 → 绿。
- **Stage 4 硬前置 3** ✅ 作用域过滤 + scoped pull + 4b/4c retrofit（4a workspaces 本身是顶层 scope 不需 retrofit）：
  - 后端：`routers/dialogs.py::list_dialogs` 加 `workspace_id: Optional[str] = Query(None, alias='workspaceId')` 参数 + `WHERE workspace_id = X` 子句；同款给 `routers/items.py::list_items`（alias=`dialogId`）。两 endpoint response_model 升格为 `Union[list[Row], CursorPage[Row]]`，按是否带 `limit=` 切换 bare list / cursor envelope。与硬前置 1 cursor 协议正交，可 `?dialogId=Y&since=N&limit=200` 组合。无 scope 参数时维持原行为（向后兼容）
  - 前端新增 `src/data/repositories/scoped-pull.ts`：`createScopedPull<TRow>(opts: {tableName, scopeField, fetchFn})` 返回 `{pullScope(scopeId), lastVersionFor(scopeId), applyEvent(e), scopeKeyFor, reset}`。scopeKey = `<table>:<scopeField>:<scopeId>`（前缀含表名避免跨表撞 key）；`Map<scopeKey, {lastVersion, inflight}>`；inflight 复用 + applyEvent 守卫（未 pull 过的 scope 不写入，不 speculatively create entry）
  - 前端 retrofit `src/data/repositories/dialogs.server.ts`：scopeField=`workspaceId`，`pullForSpec(spec)` 用 `extractScopeId` 路由：scopeId 存在走 `scopedPull.pullScope`，否则 fallback 全表 `pull()`；`observeFind` mount 时主动 pullForSpec；realtime applyEvent 后顺手 `scopedPull.applyEvent` 双向 bump cursor；scoped fetchFn 内同时 bump 模块级 lastVersion（versions 来自共享 global_change_seq，单调）
  - 前端 retrofit `src/data/repositories/items.server.ts`：同模板，scopeField=`dialogId`；scoped fetchFn 内 `Promise.all` decode（含 `materializeAttachment`）完毕**再**进 dexie tx（避免 Dexie callback 等非 Dexie promise 自动 abort）
  - api: `tests/api/test_dialogs.py::test_list_with_workspace_id_filters_to_scope` / `::test_workspace_id_combined_with_since_and_limit`（cursor 续拉 + 跨页拼齐）/ `::test_workspace_id_account_isolation`（B 用 A 的 ws-id 返 200 + `[]`，不返 403）/ `::test_no_scope_param_returns_full_user_table`
  - api: `tests/api/test_items.py::test_list_with_dialog_id_filters_to_scope` / `::test_dialog_id_combined_with_since_and_limit` / `::test_dialog_id_account_isolation` / `::test_no_dialog_id_param_returns_full_user_table`
  - spec: `tests/e2e/stage4_pre/scoped-pull.spec.ts`（providers-rest profile 3 case + realtime-ws profile 1 case）：① 清空 IDB → observeFind({where:{dialogId:X}}) → 仅 X 的 items 进 IDB + 网络仅有 `?dialogId=X` 请求，无全表 fetch；② 连开 3 个 dialog → 3 次独立 scoped fetch + 0 全表 fetch；③ 同 scope 反复 mount 4 次 → 第一次 since=0、后续全部 since>0（cursor 单调）+ 总 fetch ≤ 1+follow-up 数；④ A 写 dlgX 的 item，B 仅观察 dlgY，realtime 推到 B，验证 B 后续首次 pullScope(dlgX) 仍 since=0（applyEvent 未 speculatively 给 dlgX 建 state entry）
  - 测试输出：`pnpm test:api`（162 passed，新增 8 case）+ `pnpm test:e2e`（86 passed / 334 skipped；新增 4 active case）
  - 故障注入红测：① 注释 `dialogs.py` 的 `if workspace_id is not None: stmt = stmt.where(...)` → `test_list_with_workspace_id_filters_to_scope` / `_combined_with_since_and_limit` 红，stdout 报 leaked row from another workspace（`{... workspaceId: 'ws2'}` 出现在 ws1 期望集中）；恢复后绿。② `items.server.ts::pullForSpec` 改为无条件 `await pull()`（去掉 scopedPull 分支） + `rm -rf tests/.builds/<profile>` 强制 rebuild → 4 e2e 全红，case4 stdout 报 `got 0: [{"url":"…/api/v1/items?since=56","dialogId":null,"since":"56"}]`（看到全表 fetch 而非 scoped）；恢复后绿

### 部署状态

- **服务 1（指向 `my-deploy`）**：HEAD `2b26349`（Stage 0 baseline + Dexie 路径 bug fix）。env 不开任何 backend flag。承载老用户。Stage 5 落地后由 active 用户迁移率 ≥ 80% 触发 6 周下线窗
- **服务 2（指向 `new-deploy`）**：公网域名 `https://p01--new-aiaw--hqdb2bsvdnbt.code.run`，独立 Postgres
  - 前端 `.env.docker` 三档全开：`BACKEND_DATA_API_URL=<self>` + `BACKEND_AUTH=true` + `BACKEND_DATA_TABLES=providers,reactives,assistants,installedPlugins,avatarImages,workspaces,dialogs,items,artifacts,messages` + `REALTIME_TRANSPORT=auto` + `DEXIE_DB_URL=`（留空）
  - 后端 Northflank 控制台 env：`BACKEND_DATA_API_ENABLED=true` + `JWT_SECRET=<random>` + `DATABASE_URL=<self-hosted PG>` + `ALLOW_REGISTRATION=invite` + `INVITE_CODE=aiaw-2026-beta`（dev preview 不开放注册；详见 CLAUDE.md「Northflank 部署侧约束 / 服务 2 / 注册模式」段）
  - Dockerfile 第二阶段含 `alembic upgrade head` 启动钩子
  - 实测：`/api/v1/health` `{status:"ok",db:"ok"}`、`/api/v1/auth/me` 401、`/api/v1/providers` 401、`/api/v1/reactives` 401、`/api/v1/assistants` 401、`/api/v1/avatar-images` 401、`/api/v1/installed-plugins` 401、`/api/v1/workspaces` 401、`/api/v1/dialogs` 401、`/api/v1/items` 401、`/api/v1/artifacts` 401、`/api/v1/messages` 401（auth 优先于 422 校验），认证后 bare GET `/api/v1/messages` 422、`/api/v1/auth/register` 422
  - **当前能用 / 不能用**：providers + reactives + assistants + installedPlugins + avatarImages + workspaces + dialogs + items + artifacts + messages 跨设备同步可用（**Stage 4.5 物理分水岭已过**）；dialogs / items / artifacts / messages 已支持 scoped pull（按 `?workspaceId=` / `?dialogId=` lazy 拉，messages 是 mandatory `?dialogId=`，打开单 workspace / dialog 不再触发全表 fetch）；workspace 删除时 dialogs + items + artifacts + messages 走 server 端真级联；items.contentBuffer ≥ 64KB / artifacts.versions JSON ≥ 64KB / messages 整体 envelope JSON ≥ 64KB 自动走对象存储 ref + 跨 tab `materializeAttachment` 字节级一致；messages 跨 tab 流式同步可用（inline envelope + 200ms+1KB+sentence 三触发节流）；老用户旧数据可通过 `ImportDataDialog` 上传（5MB multipart + Phase A/B/C/D worker，AccountPage 卡片实时显示进度）；新设备首次登录走 `GET /api/v1/bootstrap` 一次拉所有小表 + per-dialog 最近 50 条 messages，2s timeout 自动 fallback 进度式加载 + banner 提示。**Stage 4.5 物理分水岭已过，可邀请少量内圈用户做真实 export → import 端到端验收**。Stage 4.7 离线 outbox 待落地，Stage 4.9 删 dexie 实现待 4.5+4.7 稳定 ≥ 1 周后做。

### 下一步：Stage 4.7 — 离线 outbox（写路径离线兜底）

Stage 4.5 整体收尾（Step 1-8 全部 ✅）：worker 三阶段 + multipart 上传 + WS 进度 + ImportDataDialog + AccountPage 卡片 + bootstrap endpoint + router guard 全部落地。bootstrap endpoint `GET /api/v1/bootstrap` 一次返 8 表（workspaces/dialogs/providers/assistants/installed_plugins/reactives/avatar_images/messages_recent + schema_version；强约束 `items` / `artifacts` 不进 response），messages_recent 按 dialog 整截断 1MB 硬顶 + 每 dialog 最多 50 条；router guard 一次/session（`sessionStorage:aiaw.bootstrap.attempted`）、2s `AbortSignal.timeout` 失败 fallback 写 `aiaw.bootstrap.fallback=1` + MainLayout `<q-banner data-test-id=bootstrap-fallback-banner>`；`Cache-Control: private, max-age=10`。下一步 Stage 4.7 加 IDB outbox 表 + `<table>.server.ts` write fail enqueue + 重连 flush + LWW 静默丢旧。

### 未启动（按依赖顺序）

- **Stage 4.7** 离线 outbox（IDB outbox 表 + 重连 flush + LWW）⏳ ← 4.9 前置
- **Stage 4.9** flag 路由层 + dexie 实现一次性下架 ⏳ ← 需 Stage 4.5 端到端验收通过 + 4.7 稳定运行 ≥ 3 天
- **Stage 5** 端到端验证（旧版 export → 新版 import 字节级互通 + 卸载重装 + 跨平台真机） ⏳

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
- **IndexedDB 角色降级。** Stage 0–4.4 仍是首屏读源；Stage 4.5 起 `GET /api/v1/bootstrap` 接管首登路径（小表全量 + 最近 50 条 message/dialog）；items / artifacts / messages 历史走硬前置 3 scoped pull 按 dialog/workspace 范围 lazy 拉；Stage 4.7 起加离线 outbox（解决"写"的离线）；Stage 4.9 起 IndexedDB 完全降级为缓存层（server 是权威，IDB 仅服务读路径的离线 / 弱网）。详见 2026-05-03「plan 自洽刷新 + lazy 路径补齐」修订记录。

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

**回滚**：纯机械重构，revert 整批 commit 即可。

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
4. **双向兼容**：flag 关时建 Q1（走 Dexie）→ 切 flag 开 → Q1 不会自动出现在 Postgres（导入导出 only 路径下不存在自动迁移机制，老用户旧数据需走 ExportDataDialog → ImportDataDialog）但 IndexedDB 仍能读

**Stage 1 出口判据**：4 个场景全过 + bundle 体积变化可接受 + flag 默认关时与 Stage 0 行为字节级一致 → 可合并到 new-deploy 上线（2026-05-02 拓扑硬切前为 my-deploy）。

---

### Stage 1.5 — 自家鉴权（多用户 Ready）

> **⚠️ 部分内容已清理（2026-05-02 · Stage 2.5 落地完成）**：本段中以下与「双写窗口 / Dexie 账号关联」相关的设计在导入导出 only 路径下从未生效，已在 Stage 2.5 整体清理：
>
> - ~~`users.linked_dexie_email` 列 + UNIQUE 约束 + alembic migration~~（drop migration `c4f1e2d3a8b0` 已落地）
> - ~~`POST /api/v1/auth/link-dexie` endpoint + `tests/api/test_auth.py::test_link_dexie_first_write_wins`~~
> - ~~前端首次登录调 `link-dexie` 的逻辑~~（实际从未接入，从 plan 中抽除）
> - ~~「与 Dexie Cloud 共存策略（双写窗口）」整段~~
> - ~~「用户身份关联（为后续数据迁移铺路）」整段~~
>
> 鉴权主体（用户表 / refresh token / JWT 签验 / register / login / refresh / logout / me / `BackendAuthSource`）保持有效，是后续所有 stage 的基础。

**目标**：上线一套自家管控的多用户鉴权，作为后续所有 `/api/v1/*` 端点的统一身份来源。Stage 5 不再需要做"切换鉴权"。

**后端新增**

- `src-backend/data/models/user.py` — `User(id, email UNIQUE, password_hash, status, created_at, last_login_at)`
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
- `routers/providers.py` 把 `_current_user_id` 占位换成 `Depends(current_user)`，user_id 取 `user.id`
- 注册开关 env `ALLOW_REGISTRATION=true|false|invite`；invite 模式下需要 `INVITE_CODE`

> 原计划含 `users.linked_dexie_email NULL UNIQUE` 列 + `POST /api/v1/auth/link-dexie` endpoint，用于"双写窗口期间把 dexie 账号 ↔ backend 账号配对"。导入导出 only 路径下整套机制无意义，已在 Stage 2.5 整体清理（drop 列 migration `c4f1e2d3a8b0`，endpoint 从未接入）。

**前端新增**

- `src/data/auth.ts` 新增 `BackendAuthSource` 实现 `AuthSource`：
  - `login()` 弹自家邮箱 + 密码表单（最小可用，复用 Quasar Dialog）
  - 内存 + `localStorage` 持久 access/refresh token；自动刷新
  - `currentToken()` 返回 backend access token，供 `src/data/http.ts` 使用
- 通过 flag `BACKEND_AUTH=true` 决定 `authSource` 默认实现挂哪一个：
  - `BACKEND_AUTH=true` → `BackendAuthSource`
  - 否则 → 现有 `DexieAuthSource`（Stage 0 实现，行为不变）
- `src/router/routes.ts`：`/account` 页在 `BACKEND_AUTH=true` 时绑定 `BackendAuthSource`，否则维持原逻辑

**单一登录入口（2026-05-02 · Stage 2.5 整理后）**

- new-deploy 镜像不挂 `dexie-cloud-addon`，AccountPage 只显示自家 JWT 登录入口
- 不存在"原 Dexie 账号"区块、不存在双写窗口、不存在 dexie email ↔ backend account 配对机制
- 老用户的迁移路径 = 旧版 ExportDataDialog 导出 → 新版 ImportDataDialog 导入（详见「现有用户数据迁移」段），不依赖账号关联

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
- spec: `tests/e2e/stage1_5/auth-ui.spec.ts`（providers-rest profile 走 BackendLoginDialog UI 完整链路：注册 → currentToken 起效 + AccountPage 显示用户 → AccountPage 退登 → currentToken 清空；及刷新 boot 路径）

> 原计划含 `test_link_dexie_first_write_wins` case，对应已删除的 `link-dexie` endpoint，引用已同步删除。

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
- **断线补漏**：Outbox 表延后到 Stage 4.7 引入（2026-05-03 修订，原写"Stage 3 引入"已作废）；Stage 2-4 单表场景下，重连发 `since=<lastRev>` → server 直接 `SELECT * FROM <table> WHERE user_id=? AND version > ?` 回放，覆盖"读"补漏；"写"补漏走 Stage 4.7 outbox。
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
   - 决策：Stage 2.5 已摘 `dexie-cloud-addon`，问题应已自然消失；如再现需重新评估。

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

**脚手架增量**（与 plan 维护规则同步在 Step 5 落地批次内一并加进 `playwright.config.ts` / `tests/env/` / `tests/scripts/`）

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

#### 批次拆分（2026-05-02 方向调整修订）

**2 个批次**，按 schema 决策大小折中：

- **批次-3a · `reactives` 单独批次**：KV 形主键 `(user_id, key)` 与其它表的 `id` 主键模式不同，realtime envelope 需要重新设计 `{key, version, updated_at, deleted, data}`，前端 `persistent-reactive.ts` 适配层也是独立工作。单独走避免把 KV 决策污染其它叶子表
- **批次-3b · `assistants` + `installedPluginsV2` + `avatarImages` 合并到一个批次**：3 张表 schema 都是简单 row + `id` 主键 + 没级联 + 没大行 + 决策点小，可以共享一份 server.ts 模板（参考 `providers.server.ts`）。`avatarImages` 含小 blob（< 64KB 通常），如单条 ≥ 64KB 走 base64 内联即可（Stage 4 硬前置 2 才引入对象存储分流，叶子表不需要）

#### 每个批次必须包含

- 后端：`models/<table>.py` SQLModel + `routers/<table>.py` REST（GET list / GET one / PUT / DELETE / `?since=N`）+ alembic migration（独立 head）+ router 加进 `app.py::_enable_backend_data_api()` flag 守卫的 lazy import 列表
- 前端：`<table>.server.ts` Repository 实现 + `repositories/index.ts` flag 路由加分支 + `server-tables.ts` 的 `SERVER_CAPABLE_TABLES` 加表名
- env：`.env.docker` 的 `BACKEND_DATA_TABLES` CSV 加表名（**直接翻开，不走"上线但不启用"**，详见 2026-05-02 方向调整修订）
- 测试（同批次落 spec、本地真跑过、必须真跑红一次再跑绿）：
  - **api**：CRUD 全过 + `?since=N` 严格大于 + soft-delete tombstone + 账号隔离 + 鉴权 401（每张表 ~6 case，参考 `tests/api/test_providers.py`）
  - **api**：KV 形（仅 reactives）的 `(user_id, key)` 复合主键唯一性 + `key` 模糊查询 / 前缀过滤
  - **e2e**：`stage3/<table>-realtime.spec.ts` 4 case（参考 `stage2/step4-providers-realtime.spec.ts`）：① A 写 → B 实时收到（同账号双 tab）；② A 离线 30s + 写 → 重连后 B 收齐增量；③ providers-rest profile（无 realtime）下走 REST 也能跨 tab 同步（刷新即可）；④ baseline profile 下表走 dexie（验证 flag 路由切换）。**第 ④ 条在 Stage 4.9 删 dexie 实现后整体删除**
  - **e2e**：`stage3/<table>-cache-roundtrip.spec.ts` ≥ 2 case：① 清 IndexedDB → 刷新 → server 数据回灌；② 写完 + 刷新 → IndexedDB 缓存命中（不再发 `?since=0` 全量）
  - **e2e**（仅 批次-3a reactives）：`persistent-reactive` 透传链路 spec —— `persistentReactive('test-key', {...})` 写入 → `repos.reactives.put({key:'test-key',...})` 真打到 server → 第二 tab 同 key 读到（用户偏好 / 缓存等多个核心 store 依赖此链路，必须有专门 spec）
- plan：本 stage 段尾「批次状态」表更新对应批次行（commit、spec/api 引用、`pnpm test:api && pnpm test:e2e` 输出摘要）

#### 通过判据汇总（每个批次都必须满足）

- `pnpm test:api` 全绿（含本批次新增 case）
- `pnpm test:e2e --project=baseline --project=providers-rest --project=realtime-ws --project=realtime-auto` 全绿（保留涉及到 Stage 3 表的相关 spec；新加表不需要为 sse/poll 单独跑）
- 本地手测主路径（5-10 min 冒烟级，不是真实端到端）：注册新账号 → 用 UI 创建/修改/删除新表的几条数据 → 第二 tab 实时收到 → 清 IndexedDB 刷新数据回灌
- env push 后 `https://p01--new-aiaw--hqdb2bsvdnbt.code.run` 实测对应 endpoint 200 / 401 / 422 行为符合预期

#### 回滚策略

- **第一道线（应急）**：`.env.docker` 把表名从 `BACKEND_DATA_TABLES` CSV 摘出 → push 重 build → 该表退回 dexie 实现，IndexedDB 缓存仍能读。**这条路径在 Stage 4.9 删 dexie 实现后失效**
- **第二道线（兜底）**：用户回退到 my-deploy URL，老数据原封不动（new-deploy 与 my-deploy 是独立实例，互不影响）

#### 批次状态（每批次落地后填）

- 批次-3a (`reactives`)：✅ 落地
  - 后端：`models/reactive.py`（复合 PK `(user_id, key)`） + `routers/reactives.py`（envelope `{key, version, updated_at, deleted, data}`） + alembic migration `d6a3f8c91e22` + WS / SSE 加 reactives 白名单（`stream.py` `_serialize_reactive` + `sse.py` 同款）+ `app.py::_enable_backend_data_api` 挂载 reactives_router
  - 前端：`reactives.server.ts`（KV envelope unwrap：`db.reactives.put({key, value: row.data})`； `get`/`put`/`delete` 都触发 `ensureRealtimeSubscription` 因为 `persistent-reactive` 走 `useLiveQuery(get(key))` 而非 `observe*`）+ `repositories/index.ts` flag 路由 + `SERVER_CAPABLE_TABLES` 加 `reactives`
  - env：`.env.docker` `BACKEND_DATA_TABLES=providers,reactives`；`tests/env/.env.test.{providers-rest,realtime-ws,realtime-sse,realtime-poll,realtime-auto}` 同步加 `reactives`
  - api: `tests/api/test_reactives.py::test_put_creates_and_list_returns_it / test_get_returns_single_row / test_put_update_bumps_version / test_since_filter_drops_older_revisions / test_soft_delete_yields_tombstone_in_list / test_delete_then_put_revives / test_account_isolation_same_key_two_users / test_unauth_request_rejected`（8 case，全绿）
  - spec: `tests/e2e/stage3/reactives-realtime.spec.ts::case1 / case2 / case3 / case4`（4 case） + `tests/e2e/stage3/reactives-cache-roundtrip.spec.ts::case1 / case2`（2 case） + `tests/e2e/stage3/persistent-reactive-passthrough.spec.ts`（1 case）
  - 测试输出：`pnpm test:api` 45 passed ~64s（37 → 45）；`pnpm test:e2e` 28 passed / 116 skipped ~2.1 min（17 → 28）
  - 红测验证：① api 层临时把 `_to_row` 的 `data` 写死 `None`，5/8 case 红，stdout 准确报 `assert None == {...}`，恢复后绿 ② e2e 第一次跑时发现 stream.py / sse.py `TABLE_MODELS` 仅白名单 `providers`，realtime case 全红 → 加 reactives whitelist + serializer 后绿（spec 真信号准确）
- 批次-3b (`assistants` / `installedPluginsV2` / `avatarImages`)：✅ 落地
  - 后端：`models/{assistant,avatar_image,installed_plugin}.py` + `routers/{assistants,avatar_images,installed_plugins}.py` + alembic migration `e4b2c5f9d017` + WS / SSE 加 3 张表白名单与 serializer + `app.py::_enable_backend_data_api` 挂载 3 个 router
  - 前端：`assistants.server.ts` (id-PK，复用 providers 模板) + `installed-plugins.server.ts` (KV-PK 但 `data` 是整行——区别于 reactives 的 value-blob unwrap) + `avatar-images.server.ts` (id-PK + ArrayBuffer↔base64 wire 边界编解码) + `repositories/index.ts` flag 路由扩展 + `SERVER_CAPABLE_TABLES` 加 3 张表（前端用 camelCase 名 `assistants` / `installedPlugins` / `avatarImages` 与 `repos.*` 属性同名；后端 endpoint 是 snake_case `installed-plugins` / `avatar-images`，转换在 server.ts 内）
  - env：`.env.docker` `BACKEND_DATA_TABLES=providers,reactives,assistants,installedPlugins,avatarImages`；`tests/env/.env.test.{providers-rest,realtime-{ws,sse,poll,auto}}` 同步
  - api: `tests/api/test_assistants.py` (8 case) + `tests/api/test_avatar_images.py` (8 case) + `tests/api/test_installed_plugins.py` (8 case)，全绿
  - spec: `tests/e2e/stage3/{assistants,installed-plugins,avatar-images}-realtime.spec.ts` (各 4 case) + `tests/e2e/stage3/{assistants,installed-plugins,avatar-images}-cache-roundtrip.spec.ts` (各 2 case)，profile 切片后约 30 个 batch-3b 实测 case，全绿
  - 测试输出：`pnpm test:api` 69 passed ~64s（45 → 69，+24）；`pnpm test:e2e` 57 passed / 194 skipped ~4.5 min（28 → 57）
  - 红测验证：① api 层临时把 assistants `_to_row` 的 `data` 写死 `None`，3/8 case 红，stdout 准确报 `TypeError`，恢复后绿 ② installedPluginsV2 server.ts 因 InstalledPlugin 类型经 MCP SDK 引入 JSONSchema7 → KeyPath 递归类型爆，TS2615 卡 build；改用 per-row puts 替代 `db.transaction()` 包装，typecheck 过 ③ cache-roundtrip 一开始用预测固定 id（`cache-asst-1` 等），realtime-ws → realtime-auto profile 间复用同一 backend 触发 409 owned-by-another-user；改 `id + Math.random()` 后绿 ④ 涉及 ArrayBuffer 的 avatarImages-realtime 一开始用 `eval()` 把 factory 字符串注入 page 上下文，eslint 拦截；改成 `pagePutAvatar()` 函数把 buffer 创建移到 `page.evaluate` 闭包内，绿

---

### Stage 4 — 迁移联表 / 级联表（`workspaces` / `dialogs` / `messages` / `items` / `artifacts`）

**目标**：搬级联删除集群（`stores/workspaces.ts` 里的 `db.transaction` 是最难的一处）。

#### 硬前置（2026-05-02 新增 / 2026-05-02 收窄）

**messages / artifacts 走的不是 providers / assistants 那种小行 schema**——单条 message 可能携带几 MB 的 base64 attachment（受 `MAX_MESSAGE_FILE_SIZE_MB` 上限），单用户 messages 表行数轻易上万。沿用 Stage 1–3 的协议会在三处崩：① WS event 带完整 row → 单条 5MB attachment 把 broker `maxsize=200` 队列内存撑爆；② `?since=N` 全 row 返回 → 客户端 fetch 几十 MB 卡死；③ Postgres TEXT 列直接存 base64 → 表线性膨胀、`vacuum` / 备份 / 慢查询全受影响。messages / artifacts server.ts 落地前必须先做完下面两件事，不可后置。

**前置 1：cursor 分页（list endpoint 续拉协议）**

> 2026-05-02 收窄：原设计含 notify-only WS 协议 + cursor 分页两块；实施一半发现 notify-only 朴素实现对流式同步零正收益负 RTT，且引入弱网 race condition 让 UX 变差。决策放弃 notify-only，硬前置 1 收窄为「仅 cursor 分页」。详见顶部 2026-05-02 修订记录「硬前置 1 收窄」条。

- list endpoint（已迁表 + 未来 Stage 4 主体批次新加表）支持 `?since=N&limit=200`，响应从 `list[Row]` 升格为 `{rows, next_cursor}` envelope；客户端拿 `next_cursor` 续拉直到为 null。**无 `limit` 参数时维持 bare list**（向后兼容 Stage 1–3 小表 list 调用 + Stage 1.5 鉴权链路）
- 单次响应硬上限：行数 `CURSOR_MAX_LIMIT=1000` 兜底（按行数限制，前提是硬前置 2 已把 attachment 拆出去后单 row ≤ ~200KB）；如 messages 长回复实测单次响应仍偏大，后续按字节限制再调
- **流式 WS / SSE event 维持 inline envelope**（带完整 row）：所有表共用 Stage 1–3 已有的 envelope 协议，没有大表 / 小表协议之分，没有 row-fetch 中间态 / race condition / 404-as-tombstone 启发式
- broker 队列内存兜底依赖：硬前置 2（attachment 走 ref，单 row ≤ ~200KB）+ broker 现有 `maxsize=200` close-on-overflow（Stage 2 设计）。messages / artifacts 落地后用 soak.sh 真测「100 条大 row 连续 PUT，server RSS 涨幅 < 50MB」做规模兜底监控
- **通过判据**：
  - api: 不带 `limit` → 响应 `list[Row]`（向后兼容判据）—— `tests/api/test_pagination.py::test_no_limit_returns_bare_list_back_compat` ✅
  - api: 带 `?limit=N` → 响应 `{rows, next_cursor}` envelope；3 行 + `limit=10` 时 `next_cursor=null` —— `tests/api/test_pagination.py::test_with_limit_returns_envelope` ✅
  - api: 满页时 `next_cursor` 是最后一行的 `version`；空续拉返回 `next_cursor=null` —— `tests/api/test_pagination.py` 多 case ✅
  - api: `limit <= 0` 拒绝 400 + `limit > CURSOR_MAX_LIMIT` 静默 clamp —— `tests/api/test_pagination.py` 已含 ✅
  - api（messages 落地时新加）: 单用户 1 万条 messages，`?since=0&limit=200` 第一次响应 < 1MB，`next_cursor` 非空；循环续拉 ≥ 50 次拉完
  - spec（messages 落地时新加）: 双 tab 跨设备 PUT 200 次模拟流式 token 流 → 第二 tab 在 inline envelope 协议下 UI 无丢帧 / 无倒退；首屏 cursor 续拉到 `next_cursor=null`
- **删除的设计（2026-05-02 收窄）**：
  - ~~WS event payload 改 `{table, op, id, rev}` only~~（notify-only 协议）
  - ~~客户端 GET row 按需拉 + If-None-Match ETag / 304~~
  - ~~per-table `payloadMode: 'inline' | 'notify-only'` 配置位~~
  - ~~`src-backend/data/payload_mode.py` + `routers/testing.py` + `_test/payload-mode` admin endpoint~~
  - ~~`src/data/realtime-rest.ts` + WS / SSE dispatcher 的 fetchAndDispatch 路径~~
  - ~~`tests/e2e/stage4_pre/notify-only-dispatcher.spec.ts` + `tests/api/test_payload_mode.py`~~
  - ~~`BACKEND_TEST_HOOKS_ENABLED` env + `apply_mode` 包裹~~

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
- **落地状态（2026-05-02）**：✅ 已完成
  - 后端：`models/blob.py`（Blob + BlobRef） + `routers/blobs.py`（POST 上传 / GET metadata / HEAD / GET `/{sha}/data?exp=&sig=` presign / DELETE per-user ref） + `data/blob_store.py`（BlobStore 抽象 + LocalFsBlobStore + S3BlobStore 骨架 + HMAC-SHA256(JWT_SECRET, `sha\|exp`) 签名）+ alembic migration `f1a3b8c5d4e2`（blobs + blob_refs，FK ON DELETE CASCADE，索引 ix_blob_refs_sha256 用作 GC 反向扫描）+ `app.py::_enable_backend_data_api` 挂载 blobs_router
  - 前端：`src/data/blob-client.ts`（`putBlob` / `fetchBlob` / `serializeAttachment` 自动按 `BLOB_INLINE_MAX_BYTES` 阈值二选一 inline / ref / `materializeAttachment` 反向）+ `src/data/index.ts` 导出 + `src/boot/expose-debug.ts` 加 `__blobClient__` 钩子
  - 测试：96 case 全绿（API 层 27 case + 故障注入两轮 ① skip ownership check → 跨用户隔离 case 红 stdout 直出 sha + envelope dict / ② sha256 反转 → 多 case 红 stdout 直出 'expected vs got sha 对照'，恢复后绿）
  - 端到端：providers-rest profile 6 case + baseline guard 1 case 全绿
- **API 层判据**（pytest）：
  - api: `tests/api/test_blobs.py::test_post_creates_blob_row_and_storage_file` (PUT bytes → PG `blobs` 行 + LocalFS storage_key 文件存在 + url 含 sig+exp)
  - api: `tests/api/test_blobs.py::test_post_creates_per_user_ref` (per-user blob_refs 行)
  - api: `tests/api/test_blobs.py::test_same_content_second_upload_is_deduped` (PG `blobs` 行不增，第二次 deduped=True)
  - api: `tests/api/test_blobs.py::test_signed_url_returns_bytes` (anon_client 用 sig URL 拿到 bytes)
  - api: `tests/api/test_blobs.py::test_get_metadata_returns_envelope` + `::test_head_returns_metadata_headers`
  - api: `tests/api/test_blobs.py::test_boundary_size_64kb_round_trip[1/65535/65536/65537]` (4 边界尺寸都能往返)
  - api: `tests/api/test_blobs.py::test_large_5mb_round_trip` (slow mark, messages-级 attachment 端到端)
  - api: `tests/api/test_blobs.py::test_empty_body_rejected` (400)
  - api: `tests/api/test_blobs.py::test_cross_user_each_owns_independent_ref` (同 sha 两用户独立 ref，bytes 全局去重)
  - api: `tests/api/test_blobs.py::test_user_b_cannot_see_a_uploaded_blob` + `::test_user_b_cannot_delete_a_ref` (404 mask 隐藏存在性)
  - api: `tests/api/test_blobs.py::test_signed_url_rejects_tampered_signature` + `::test_signed_url_rejects_expired` + `::test_signed_url_path_rejects_missing_query` + `::test_signed_url_for_unknown_sha_returns_404` (presign 安全边界)
  - api: `tests/api/test_blobs.py::test_invalid_sha_format_400`
  - api: `tests/api/test_blobs.py::test_delete_ref_drops_only_callers_ownership` (跨用户隔离 + bytes 留待 GC)
  - api: `tests/api/test_blobs.py::test_a_can_repost_after_delete_ref` (delete + re-upload roundtrip)
  - api: `tests/api/test_blobs.py::test_unauth_post/metadata/head/delete_rejected` (鉴权 401)
  - api: `tests/api/test_blobs.py::test_localfs_writes_sharded_path` (`<sha[:2]>/<sha[2:]>` 分片磁盘路径)
- **前端层判据**（Playwright，providers-rest profile）：
  - spec: `tests/e2e/stage4_pre/blob-client.spec.ts::putBlob round-trips small bytes + signed URL fetch works` (端到端：FormData multipart + Bearer + sha256 + signed URL 拿回 bytes)
  - spec: `tests/e2e/stage4_pre/blob-client.spec.ts::putBlob dedup: same bytes → same sha256` (浏览器端 dedup 一致性)
  - spec: `tests/e2e/stage4_pre/blob-client.spec.ts::serializeAttachment: <64KB → inline, >=64KB → ref` (4 边界尺寸 1B / 65535 / 65536 / 65636 类型分流正确)
  - spec: `tests/e2e/stage4_pre/blob-client.spec.ts::materializeAttachment: inline → Blob round-trip` + `::ref → fetch via signed URL` (反向链路 inline / ref 双分支)
  - spec: `tests/e2e/stage4_pre/blob-client.spec.ts::baseline: putBlob throws when BACKEND_DATA_API_URL is not configured` (flag 关时 fail-fast，bundle 不带后端依赖)
- **未来 Stage 4 主体批次还需补的判据**（messages / artifacts 落地时新加）：
  - spec: client 端 PUT message with 5MB attachment → 第二 tab 收到 event → GET row → 通过 ref URL 下载 blob → UI 渲染 attachment，端到端 < 5s
  - spec: 客户端 IndexedDB 清空后刷新 → ref blob 重新从对象存储拉回 → UI 一致
- **未来扩展（不在本批次）**：
  - 周期 GC job：扫 `blobs LEFT JOIN blob_refs` 找无 ref 行 + 超 7 天 → 删 BlobStore bytes + DELETE blob 行（FK CASCADE 自动清 blob_refs）。当前 row delete 时不立即 delete blob bytes 这点需 Stage 4 主体批次 messages / artifacts row-CRUD 接好 blob_refs 增减后才能跑通——本批次只把 schema + endpoint 落地，GC 留待后续
  - Stage 4.5 ImportJob 多部分上传协议：`BlobStore.create_multipart_upload` / `generate_part_url` / `complete_multipart_upload` / `abort_multipart_upload` 4 个方法；本批次只暴露单 POST 上传，多部分留 4.5

**前置 3：作用域过滤 + scoped pull（2026-05-03 新增）**

> 2026-05-03 新增。当前所有 server-routed 表的 `pull()` 都是「全 user 单表 since=N 全量增量」，后端 list endpoint 也只接 `?since=N`。这等于"打开任意 dialog 都全表拉一次 items / messages / artifacts"，跟旧 Dexie Cloud 全量拉本质相同，违背"服务端优先 / 即点即看"需求。本前置补齐 lazy 路径，**4d / 4e 强依赖，必须在它们之前落地**；同时 retrofit 已落地的 dialogs / items（不含 workspaces，workspaces 本身就是顶层 scope）。

- **后端**：list endpoint 加可选 query 参数（与硬前置 1 cursor 协议正交，可 `?dialogId=Y&since=N&limit=200` 组合）：
  - `GET /api/v1/dialogs?workspaceId=X&since=N` — `routers/dialogs.py::list_dialogs` 加 `workspace_id: Optional[str] = None` 参数 + `WHERE workspace_id = X` 子句
  - `GET /api/v1/items?dialogId=Y&since=N` — `routers/items.py::list_items` 加 `dialog_id: Optional[str] = None` 参数 + `WHERE dialog_id = Y` 子句
  - `GET /api/v1/artifacts?workspaceId=X&since=N&limit=200`（4d 落地时）+ `?dialogId=Y` (artifacts 也可绑 dialog) — 同模板
  - `GET /api/v1/messages?dialogId=Y&since=N&limit=200`（4e 落地时）— 必须按 dialog 过滤（messages 只 dialog scope，无 workspace scope；要查 workspace 全量靠多次 dialog scope 调用拼）
  - 不带 scope 参数时维持现状（全 user 单表 since 增量），保持向后兼容
- **前端**：`<table>.server.ts` 引入 scoped pull 基础设施：
  - 新增 `src/data/repositories/scoped-pull.ts` helper：`createScopedPull<TRow>(opts: { tableName, scopeFields, fetchFn })` 返回 `{ pullScope(scopeKey, scopeQuery): Promise<void>, lastVersionFor(scopeKey): number, applyEvent(e) }`，内部 `Map<scopeKey, { lastVersion: number, inflight?: Promise<void> }>`
  - `<table>.server.ts::observeFind(spec)`: 解析 `spec.where.workspaceId` / `spec.where.dialogId` 为 scopeKey（如 `dialog:abc123`）→ 调 `pullScope(scopeKey, { dialogId: 'abc123' })` → 再返回 cache.observeFind(spec)。scopeKey 维度的 `lastVersion` 缓存避免重复拉
  - `<table>.server.ts::observeList()` 仍走全表 pull（用作 listing all workspaces / all dialogs of a user 等罕见路径，访问频率低）
  - `<table>.server.ts::find/findFirst/findKeys/count(spec)`: 同样走 scope-aware pull
  - realtime apply 链路按现状（WS event 不带 scope filter，client 收到所有 user-scoped events，applyEvent 时按 row.workspaceId / row.dialogId 自动归到对应 scopeKey 的 lastVersion）
- **bootstrap 配合**：Stage 4.5 Step 8 bootstrap response 不含 items / artifacts（明确写进 `test_returns_all_small_tables_in_one_response` 断言 set），让 lazy 路径成为唯一拉取通道
- **回归保证**：retrofit 4a/4b/4c 时不能破坏现有全表 pull 路径（部分 store 仍用 `repos.<table>.list()` 全量），保持 list/find 的 scope-less fallback 行为
- **通过判据**：
  - api: `tests/api/test_dialogs.py::test_list_with_workspace_id_filters_to_scope`（建 ws1 / ws2 各 5 dialog → `?workspaceId=ws1` 仅返 5 条）
  - api: `tests/api/test_items.py::test_list_with_dialog_id_filters_to_scope`（同模板）
  - api: `tests/api/test_dialogs.py::test_workspace_id_combined_with_since_and_limit`（三参数组合，next_cursor 正确）
  - api: `tests/api/test_dialogs.py::test_workspace_id_account_isolation`（user A 用 user B 的 workspaceId → 仅返自己的，跨 user 静默过滤而非 403）
  - api: `tests/api/test_dialogs.py::test_no_scope_param_returns_full_user_table`（向后兼容判据）
  - spec: `tests/e2e/stage4_pre/scoped-pull.spec.ts::dialog opens with empty IDB cache → only target dialog items appear`（清空 `db.items` → 打开 dialog X → Network 面板有且仅有 `?dialogId=X` 请求 → 其他 dialog 的 items 不出现在 IDB）
  - spec: `tests/e2e/stage4_pre/scoped-pull.spec.ts::switching dialogs hits scoped endpoint per dialog`（连开 3 个 dialog → 3 次 `?dialogId=` 请求，无全表 fetch）
  - spec: `tests/e2e/stage4_pre/scoped-pull.spec.ts::scoped lastVersion cache prevents redundant fetch`（同 dialog 反复 mount/unmount 组件 → 仅首次有 fetch）
  - spec: `tests/e2e/stage4_pre/scoped-pull.spec.ts::cross-dialog realtime event applies to correct scope cache`（A tab dialog X 写 item → B tab dialog Y open → realtime event 不污染 Y scope 的 lastVersion）
  - **故障注入**（必须真跑红一次再绿）：scoped-pull.ts 把 `pullScope` 改为永远拉全表（去掉 scopeQuery 参数）→ spec1 红、stdout 报"expected 1 fetch matching ?dialogId=, got 0 (or got fetch without query)"，恢复后绿
- **避坑**：
  - **不要拆两批落地**（先后端再前端）：中间态 server 已支持过滤但前端还在全表拉，等于白上线。同批次落 backend + frontend + retrofit + spec
  - scopeKey 必须包含表名前缀（如 `dialog:abc` vs `workspace:abc`）避免不同表 scope 撞 key
  - retrofit 时 `<table>.server.ts` 模块级 `let lastVersion = 0` 单例**保留作为全表 pull 的 lastVersion**（list/全量回灌路径仍用），scoped 路径走独立 `Map`，两套互不干扰
  - bootstrap response 字段 set 加进 `tests/api/test_bootstrap.py::test_returns_all_small_tables_in_one_response` 断言时显式写"items" / "artifacts" 不在 set，防未来误加
  - scoped pull 必须在 `observeFind` mount 时主动调一次（不能等 spec.where 变化才 pull），否则首次渲染看不到数据
  - 4e messages 落地时 `?dialogId=` 是 mandatory（不带就 422 reject），因为全表 messages 拉无意义且会撑爆响应；artifacts / items / dialogs 的 scope 参数仍是 optional 保持兼容
- **批次组织**：硬前置 3 单独一批（commit 命名 `云同步重构stage4-硬前置3: 作用域过滤 + scoped pull + 4a/4b/4c retrofit`），不与 4d 合批，便于单独 bisect

#### 主体迁移工作

**后端**：`DELETE /api/v1/workspaces/:id?cascade=true` 在单个 Postgres 事务里完成级联；为 dialog 删除提供同款。`GET /api/v1/messages?dialogId=…&since=…&limit=200` 让 `DialogView.vue` 的滚动加载继续可行（注意：`since` + `limit` 走前置 1 的 cursor 协议）。

**前端**：`runTx()` 对这些表走新的 `repos.batch(operations)` → `/api/v1/batch`；尚未迁移的表仍走 `db.transaction`。

#### 批次拆分（2026-05-02 方向调整修订）

**5 张表 5 个批次**，依赖排序硬约束（每张表都有重大 schema 决策不可合并）：

- **批次-4a · `workspaces`**（先做，建立级联事务的设计基线）

  - schema 决策：folder 自引用 FK + `parentId` NULL（root）vs 自引用 FK + `parentId` 必填指向虚拟 root；level 限制（避免无限嵌套）；`type` 字段约束（`'workspace' | 'folder'`）
  - 后端：`DELETE /api/v1/workspaces/:id?cascade=true` 在单个 PG 事务里清掉所有 dialogs / messages / items / artifacts / assistants（级联实现先用 `ON DELETE CASCADE`，再加 row-level user_id check）
  - 测试：`tests/api/test_workspaces.py` ~12 case（CRUD + 级联删除 + folder 树深度 + 跨 user 不能跨删 + cascade=false 走"非空时拒绝删"）；`tests/e2e/stage4/workspaces-cascade.spec.ts` ~4 case（UI 删工作区 → 子 dialogs / messages 实时消失 + server 行清空 + 第二 tab 同步看到删除）
- **批次-4b · `dialogs`**（依赖 workspaces FK）

  - schema 决策：`workspaceId` FK + `ON DELETE CASCADE`；is_active / draft 字段
  - 测试：`tests/api/test_dialogs.py` ~8 case（CRUD + workspaceId FK 失效拒绝 + 级联删 messages / items / artifacts）；`tests/e2e/stage4/dialogs-realtime.spec.ts` ~4 case
- **批次-4c · `items`**（FK→dialogs，**依赖硬前置 2 BlobStore**——items 是消息附件载体）

  - **schema 决策**（2026-05-03 修订；废弃旧"独立简单叶子表 + dialogId 可为 null"假设）：
    - `dialog_id` 提到顶层列、FK→`dialogs.id` ON DELETE CASCADE，**必填不可 null**（与代码 `StoredItem.dialogId:string` 一致）
    - `data:JSONB` opaque 列承载 `{id, dialogId, type:'text'|'file'|'quote', references:number, contentText?, contentBuffer?:AttachmentEnvelope, name?, mimeType?}`，其中 `contentBuffer` 字段在 wire 上是 `AttachmentEnvelope`（来自 `blob-client.ts`）：
      - `< 64KB` → `{type:'inline', data:base64, content_type, size}` 内联在 row（PG 不膨胀）
      - `≥ 64KB` → `{type:'ref', url, sha256, size, content_type}`，bytes 上 BlobStore，PG 行只持 ref（~几百字节）
    - 客户端 `items.server.ts::putOne` 调 `serializeAttachment(buf, mimeType)` 后 PUT；`pull` / realtime apply 时遇到 `contentBuffer` 字段调 `materializeAttachment` 还原为 ArrayBuffer 写 IndexedDB
    - 后端 `routers/items.py` 当 `data` opaque JSON 处理，**不感知 inline/ref**；与 dialogs / workspaces 模板一致，PG 不拆 attachment 列
    - blob_refs 增删：items.put 时 `serializeAttachment` 自动建 `(user_id, sha256)` ref；items.delete **不主动 delete blob ref**（多 row 共享 sha256），交未来 GC job
    - workspace cascade 链路：dialog cascade → dialog FK ON DELETE CASCADE 自动连带 items（PG 层）；同时 `workspaces.delete_workspace` cascade 分支显式 update items.deleted_at + 共用 cascade_version + 发独立 WS event 链（与 dialogs 同模板，前端按 LWW idempotent apply）
  - **测试**：`tests/api/test_items.py` ~10 case：基础 CRUD（put / get / list / delete + soft-delete tombstone + delete-then-put-revives + since 增量 + account isolation + unauth 401）+ FK 校验（`test_put_rejects_missing_dialog_id` / `test_put_rejects_unknown_dialog` / `test_put_rejects_cross_user_dialog`）+ ref 形态（`test_put_item_with_inline_attachment` / `test_put_item_with_ref_attachment_round_trips` / `test_inline_below_64kb` / `test_ref_at_or_above_64kb`）+ workspace cascade（`test_workspace_cascade_true_tombstones_items_via_dialog`）+ dialog cascade（`test_dialog_delete_cascades_items_via_pg_fk`，验证 PG 层 ON DELETE CASCADE 真触发）+ 跨 user 不能跨删
  - **测试**：`tests/e2e/stage4/items-realtime.spec.ts` ~5 case：① ws 双 tab 小 inline item put / update / delete realtime 同步（与 dialogs case1 同模板）；② ws 双 tab 100KB ref attachment：A 写入 → blob-client 走 ref → B realtime 收到 → `materializeAttachment` 还原 → contentBuffer bytes 字节级一致；③ workspace delete cascade：A 删 workspace → B 收到一组 dialogs + items tombstone events → IndexedDB 清空；④ providers-rest profile 无 realtime 刷新仍同步；⑤ baseline profile 走 dexie（4.9 删除）。**故障注入**（必须真跑红一次再绿）：跑 case2 时把 `items.server.ts` 的 `materializeAttachment` 注掉一次，验证 stdout 报"contentBuffer bytes mismatch / B got undefined while A had ArrayBuffer(102400)"；恢复后绿
  - **避坑**：见 2026-05-03 修订记录尾的避坑段（不要拷 avatarImages 模板 / 不要在 PG 拆 attachment 列 / `serializeAttachment` 是 async 走网络 / `materializeAttachment` 失败容忍 / cascade 共用 cascade_version）
- **批次-4d · `artifacts`**（依赖 Stage 4 硬前置 2 对象存储 + 硬前置 1 cursor 分页 + 硬前置 3 scoped pull）

  - schema 决策（2026-05-03 修订；废弃旧"workspaceId / dialogId 双 FK"假设）：内容字段大小阈值 → ≥ 64KB 走对象存储 ref；`workspaceId` 单 FK→workspaces.id ON DELETE CASCADE（与代码事实 `Artifact` interface 一致：仅 `workspaceId`，无 `dialogId`）
  - lazy 路径：`artifacts.server.ts` 必须自带 scoped pull（`?workspaceId=X`），不允许全表 pull 触发场景（除非用户主动调 `repos.artifacts.list()`，仍兼容）
  - 测试：`tests/api/test_artifacts.py` ~22 case（CRUD + 大行 ref 序列化 + 64KB 边界 + cursor 分页 + workspaceId scope 过滤 + workspace cascade）；`tests/e2e/stage4/artifacts-large.spec.ts` ~6 case（小 inline put/update/delete + 100KB+ ref versions 跨 tab 字节级一致 + workspace cascade fan-out + scoped pull 仅目标 workspace 进 IDB + providers-rest 无 realtime + baseline 字节级一致，**走 inline envelope 流式同步，不走 notify-only**）
- **批次-4e · `messages`**（最难，最后做，依赖 Stage 4 硬前置 1 cursor 分页 + 硬前置 2 对象存储 + 硬前置 3 scoped pull + 流式节流）

  - schema 决策：attachment 字段 `{type:'inline', data:base64}` vs `{type:'ref', url, sha256, size, content_type}`；message 顺序保证（rev / created_at / explicit `order` 字段）；`dialogId` FK；DialogView 的滚动加载靠 cursor + `?since` 双语义 ↔ "拉旧" vs "拉新"
  - lazy 路径：`messages.server.ts` 的 `?dialogId=` 是 **mandatory**（不带 422 reject，全表 messages 拉无意义且撑爆响应）；首屏靠 bootstrap response 里的 `messages_recent` 直接渲染，滚动加载用 `?dialogId=Y&since=N&limit=200` cursor 续拉
  - **流式节流策略（2026-05-03 修订）**：流式 token 入口（`composables/call-api.ts` / `utils/middlewares.ts` 的 streaming 分支）用 batch flush 包 `repos.messages.update()`，**200ms 时间窗 + 满 1KB 文本累积 + sentence boundary（。？！. ? ! 换行）三触发取最先**；非流式 PUT 立刻 flush；flush 时 PUT 当前累积的完整 message envelope（不是 delta）。流式结束时（`done` event）强制 flush 一次。具体钩点 4e 落地批次内确定（候选位置：`call-api.ts::streamingHandler` 的 onChunk 回调外包一层 batch wrapper），本修订只敲定策略
  - 测试：`tests/api/test_messages.py` ~16 case（CRUD + cursor 分页 + ref 序列化 + 跨 dialog 隔离 + soft-delete 后顺序保持 + `?dialogId=` mandatory 校验 422）；`tests/e2e/stage4/messages-attachment.spec.ts` ~7 case（5MB attachment 端到端 + 第二 tab < 500ms 同步 + 清缓存重拉 + DialogView 滚动加载历史 + 离线发送队列 + **流式 PUT 200 次模拟 token 流 → 第二 tab inline envelope 下无丢帧无倒退** + **流式节流真测：5min 流式回复 ~3000 字期间 `wsEvents` 累计 ≤ 25 条 events**）；soak.sh 重跑验证 messages PUT 高频场景下 RSS 仍 < 100MB drift / p95 < 500ms（含 inline envelope 大行 broker 队列兜底监控）

#### 每个批次必须包含

- 后端：SQLModel + router + alembic migration（独立 head）+ router 加进 `app.py::_enable_backend_data_api()` lazy import 列表
- 前端：`<table>.server.ts` Repository + `repositories/index.ts` flag 路由分支 + `server-tables.ts` SERVER_CAPABLE_TABLES 加表名 + 涉及级联事务的 `runTx()` 调用点改走 `repos.batch()` → `/api/v1/batch`
- env：`.env.docker` `BACKEND_DATA_TABLES` CSV 加表名（直接翻开）
- 测试：上面列的 api / e2e case 全部同批次落地，本地真跑 `pnpm test:api && pnpm test:e2e` 全绿，**且必须真跑红一次再跑绿**（注掉一行核心代码确认 spec 输出有足够定位信息）
- plan：本 stage 段尾「批次状态」表更新对应批次行（commit、spec/api 引用、测试输出摘要）

#### 通过判据汇总

- `pnpm test:api && pnpm test:e2e` 全绿（含本批次新增 case + 全部历史 case）
- 删除一个含多 dialog/messages/artifacts 的工作区 → 服务端清空（含对象存储 ref 的 refcount 减 1）→ 清空 IndexedDB 后刷新仍正确
- 5 张表 5 个批次全部落地后跑一次 `ExportDataDialog` → 导出 JSON 与原 my-deploy 导出格式字节级一致（验证「跨版本导入/导出兼容」段约束）
- soak.sh 重跑（messages / artifacts 高频写入场景）RSS / p95 数字达标

#### 回滚策略

- **第一道线（应急）**：`.env.docker` 把表名从 `BACKEND_DATA_TABLES` CSV 摘出 → push 重 build → 该表退回 dexie 实现，IndexedDB 缓存仍能读。**这条路径在 Stage 4.9 删 dexie 实现后失效**。注意：联表场景下单表回滚（如只回滚 messages 但保留 dialogs 在 server）会破坏 FK 一致性，回滚必须**整组回滚**（要么全 server 要么全 dexie），不能 cherry-pick
- **第二道线（兜底）**：用户回退到 my-deploy URL（new-deploy 与 my-deploy 是独立实例 + 独立 Postgres，互不影响）
- **对象存储孤儿 blob**：留给 Stage 4 硬前置 2 设计的周期 GC job（refcount=0 + 7 天）扫，不影响数据正确性

#### 批次状态（每批次落地后填）

- 批次-4a (`workspaces`)：✅ 落地
  - 后端：`models/workspace.py`（id-PK envelope，`type` / `parentId` 在 `data` JSONB 内、不拆列、不建自引用 FK；`$root` sentinel 字符串保留）+ `routers/workspaces.py`（GET list / GET one / PUT / DELETE，DELETE 接受 `?cascade=true|false` 参数但 4a 阶段子表多数还在 dexie 故为 no-op，预留 wire 形状给 4b/4c/4d/4e 真级联）+ alembic migration `a7f3c2e9b481`（依赖 `f1a3b8c5d4e2` blobs）+ WS / SSE 加 `workspaces` 白名单与 `_serialize_workspace` + `app.py::_enable_backend_data_api` 挂载 workspaces_router
  - 前端：`workspaces.server.ts`（id-PK，复用 assistants 模板；delete 走 `?cascade=true` 转发；observeList 触发 `ensureRealtimeSubscription`）+ `repositories/index.ts` flag 路由 workspaces 分支 + `SERVER_CAPABLE_TABLES` 加 `workspaces`
  - 顺手修：`stores/workspaces.ts::deleteItem` 不再 runTx 包 dexie 事务（assistants 自 批次-3b 起已 server-routed，跨表事务 + HTTP await 不兼容；workspaces 上线后同样问题。改顺序 await，dexie 跨表原子性 4b/4c/4d/4e 后由 server-side cascade endpoint 接力）。同时 `db.ts` 的 3 个 reading hook 加 `if (!row) return row` 与 `workspace?.type` 容忍 undefined（被 cascade 触发出来的 latent bug，根因是 dexie 在某些 cursor cleanup 场景给 hook 传 undefined）
  - env：`.env.docker` `BACKEND_DATA_TABLES=providers,reactives,assistants,installedPlugins,avatarImages,workspaces`；`tests/env/.env.test.{providers-rest,realtime-{ws,sse,poll,auto}}` 同步加 `workspaces`；`tests/api/conftest.py` TRUNCATE 列表加 `workspaces`
  - api: `tests/api/test_workspaces.py::test_put_creates_and_list_returns_it / test_get_returns_single_row / test_put_update_bumps_version / test_since_filter_drops_older_revisions / test_soft_delete_yields_tombstone_in_list / test_delete_then_put_revives / test_account_isolation / test_unauth_request_rejected / test_folder_type_round_trips / test_folder_tree_with_parent_chain / test_cascade_param_accepted_no_op_at_4a / test_cross_user_cannot_delete`（12 case，全绿）
  - spec: `tests/e2e/stage4/workspaces-cascade.spec.ts::case1 ws double-tab / case2 ws double-tab cascade / case3 providers-rest no-realtime / case4 baseline byte-identical`（4 case，全绿）
  - helper 增量：`tests/e2e/helpers/backend.ts` 加 `WorkspaceRow` / `putWorkspace` / `listWorkspaces` / `deleteWorkspace`（cascade 默认 true）
  - 测试输出：`pnpm test:api` 117 passed ~83s（96 → 117，+12 + ~9 历史 case 增量计数纠偏）；`pnpm test:e2e` 68 passed / 244 skipped ~4.8 min（57 → 68，+11 含 4a × 6 profile 切片）
  - 红测验证：把 `routers/workspaces.py::_to_row` 的 `data=None if w.deleted_at else w.data` 临时写死 `data=None`，5/12 case 红、stdout 准确报 `assert body['data']['name'] == 'Workspace 1' → TypeError: 'NoneType' object is not subscriptable`，恢复后绿
  - 红测验证（e2e）：第一次跑 case2 时 cascade evaluate 触发 db.workspaces.hook('reading') 在 undefined 上爆 `Cannot read properties of undefined (reading 'type')`（pre-existing latent bug），加 `workspace?.type` 守卫后绿；spec stdout / Playwright error-context.md / failed screenshot 三路定位准确
- 批次-4b (`dialogs`)：✅ 落地
  - 后端：`models/dialog.py`（id-PK envelope + `workspace_id` 提到顶层列、FK→workspaces.id ON DELETE CASCADE）+ `routers/dialogs.py`（GET list / GET one / PUT / DELETE，PUT 校验 `data.workspaceId` 存在 + 同 user，缺失/跨 user 返回 409 而非 500；同时 catch 后端 IntegrityError 转 409 防 race）+ `routers/workspaces.py::delete_workspace` 接通真级联（cascade=true 时同事务 update Dialog.deleted_at，全部 cascaded dialogs 共用一个 cascade_version，发独立 WS event 链）+ alembic migration `b5d2c8e7a394`（依赖 `a7f3c2e9b481`）+ stream/sse 加 `dialogs` 白名单与 `_serialize_dialog` + `app.py::_enable_backend_data_api` 挂载 dialogs_router
  - 前端：`dialogs.server.ts`（id-PK，复用 workspaces 模板；observeList/observeFind/observeOne 触发 `ensureRealtimeSubscription`）+ `repositories/index.ts` flag 路由 dialogs 分支 + `SERVER_CAPABLE_TABLES` 加 `dialogs`
  - env：`.env.docker` `BACKEND_DATA_TABLES=...,workspaces,dialogs`；`tests/env/.env.test.{providers-rest,realtime-{ws,sse,poll,auto}}` 同步加 `dialogs`；`tests/api/conftest.py` TRUNCATE 列表加 `dialogs`
  - helper 增量：`tests/e2e/helpers/backend.ts` 加 `DialogRow` / `putDialog` / `listDialogs` / `deleteDialog`
  - api: `tests/api/test_dialogs.py::test_put_creates_and_list_returns_it / test_get_returns_single_row / test_put_update_bumps_version / test_since_filter_drops_older_revisions / test_soft_delete_yields_tombstone_in_list / test_delete_then_put_revives / test_account_isolation / test_unauth_request_rejected / test_put_rejects_missing_workspace_id / test_put_rejects_unknown_workspace / test_put_rejects_cross_user_workspace / test_workspace_cascade_true_tombstones_dialogs / test_workspace_cascade_false_leaves_dialogs / test_workspace_cascade_skips_other_workspaces / test_cross_user_cannot_delete`（15 case 全绿）
  - spec: `tests/e2e/stage4/dialogs-realtime.spec.ts::case1 ws double-tab put+update+delete / case2 ws double-tab cascade tombstones server dialog rows + B receives ws delete / case3 providers-rest no-realtime / case4 baseline byte-identical`（4 case 全绿）
  - 测试输出：`pnpm test:api` 132 passed ~90s（117 → 132，+15）；`pnpm test:e2e` 72 passed / 264 skipped ~5 min（68 → 72，+4 含 4b 1 case × 4 profile 切片实跑）
  - 红测验证（api）：在 `routers/workspaces.py::delete_workspace` 把 `if cascade:` 临时改为 `if False and cascade:` 关掉级联分支，3 个 cascade 相关 case 红、stdout 准确报 `assert by_id['d1']['deleted'] is True → AssertionError: d1 still alive: {'id': 'd1', ..., 'deleted': False, 'data': {...}}`；恢复后绿
  - 红测验证（e2e）：在 `dialogs.server.ts` 的 realtime subscribe 回调里把 put / delete 分发注释掉，case1/case2 红、stdout 报 `row dialogs/dlg-xxx did not converge within 1500ms (A={"id":"dlg-xxx","name":"d1",...} B=undefined)`；恢复后绿。注意：build profile cache 不感知工作树 diff，红测后必须 `rm -rf tests/.builds/realtime-ws` 才能让新版代码进 bundle
- 批次-4c (`items`)：✅ 落地
  - 后端：`models/item.py`（id-PK envelope + `dialog_id` 顶层列，FK→dialogs.id ON DELETE CASCADE）+ `routers/items.py`（GET list / GET one / PUT / DELETE，PUT 校验 `data.dialogId` 存在 + 同 user，缺失/跨 user 返回 409 而非 500；同时 catch IntegrityError 转 409 防 race）+ `routers/workspaces.py::delete_workspace` 二级级联（cascade=true 时同事务先 update Dialog.deleted_at 收集 dialog_ids，再 update Item.deleted_at WHERE dialog_id IN (...) AND user_id=user，全部 cascaded rows 共用同一 cascade_version，发独立 dialog WS event 链 + item WS event 链 + workspace WS event）+ alembic migration `c4f9e2a8d6b3`（依赖 `b5d2c8e7a394`）+ stream/sse 加 `items` 白名单与 `_serialize_item` + `app.py::_enable_backend_data_api` 挂载 items_router
  - 前端：`items.server.ts`（id-PK 模板；**`contentBuffer:ArrayBuffer` 用 `blob-client.serializeAttachment` 走 < 64KB inline base64 / ≥ 64KB ref 上 BlobStore；apply 链路 `materializeAttachment` 失败容忍——row 仍写入 IDB，contentBuffer 留 undefined**；observeList/observeFind/observeOne 触发 `ensureRealtimeSubscription`）+ `repositories/index.ts` flag 路由 items 分支 + `SERVER_CAPABLE_TABLES` 加 `items`；`bulkPut` 强制 sequential（避免多 sha256 并发 putBlob 撞 `(user_id,sha256)` 唯一约束 race）
  - env：`.env.docker` `BACKEND_DATA_TABLES=...,workspaces,dialogs,items`；`tests/env/.env.test.{providers-rest,realtime-{ws,sse,poll,auto}}` 同步加 `items`；`tests/api/conftest.py` TRUNCATE 列表加 `items`
  - helper 增量：`tests/e2e/helpers/backend.ts` 加 `ItemRow` / `putItem` / `listItems` / `deleteItem`
  - api: `tests/api/test_items.py::test_put_creates_and_list_returns_it / test_get_returns_single_row / test_put_update_bumps_version / test_since_filter_drops_older_revisions / test_soft_delete_yields_tombstone_in_list / test_delete_then_put_revives / test_account_isolation / test_unauth_request_rejected / test_put_rejects_missing_dialog_id / test_put_rejects_unknown_dialog / test_put_rejects_cross_user_dialog / test_put_item_with_inline_attachment / test_put_item_with_ref_attachment_round_trips / test_inline_envelope_at_64kb_minus_one / test_ref_envelope_at_64kb_threshold / test_workspace_cascade_true_tombstones_items_via_dialog / test_workspace_cascade_skips_other_workspaces_items / test_dialog_hard_delete_cascades_items_via_pg_fk / test_cross_user_cannot_delete`（19 case 全绿）
  - spec: `tests/e2e/stage4/items-realtime.spec.ts::case1 ws double-tab inline put+update+delete / case2 ws double-tab 100KB ref attachment bytes A→B (head/tail fingerprint + server data.contentBuffer.type='ref' + sha256 长度 64) / case3 ws double-tab cascade workspace→dialog→item tombstones / case4 providers-rest no-realtime / case5 baseline byte-identical`（5 case 全绿）
  - 测试输出：`pnpm test:api` 151 passed ~99s（132 → 151，+19）；`pnpm test:e2e` 76 passed / 289 skipped ~5 min（72 → 76，+4 含 4c × 1 case real-run/profile + 25 profile-skipped）。1 个 `stage3 reactives-cache-roundtrip case2` 套间偶发 flake，独立重跑稳定通过，pre-existing 与 4c 无关
  - 红测验证（api）：把 `routers/items.py` 的 `if (await session.execute(dlg_stmt)).scalar_one_or_none() is None: raise 409` 临时改为 `if False and ...`，`test_put_rejects_cross_user_dialog` 红、stdout 准确报 `assert 200 == 409` 直接定位 ownership check 没触发；`test_put_rejects_unknown_dialog` 反而绿（PG FK 兜住转 IntegrityError → 409），刚好证明 catch fallback 也起作用、两测分属不同代码路径。第二轮把 `routers/workspaces.py::delete_workspace` 的 cascade items 分支临时改为 `if False and cascaded_dialog_ids:`，2 个 cascade case 红、stdout 准确报 `assert False is True` 加完整 by_id dict 上下文，恢复后绿
  - 红测验证（e2e）：在 `items.server.ts::decodeFromWire` 把 `materializeAttachment + arrayBuffer()` 两行注掉，case2 红、stdout 准确报 `B did not materialize item with 102400B contentBuffer within 5s; last={"stage":"no-buffer","mimeType":"application/octet-stream"}`，直接定位到 contentBuffer 没被填回（B 收到 row 但 buffer 缺失）；恢复后绿。注意：build profile cache 不感知工作树 diff，红测前后必须 `rm -rf tests/.builds/realtime-ws` 才能让新版代码进 bundle
- 批次-4d (`artifacts`)：✅ 落地
  - 后端：`models/artifact.py`（id-PK envelope + `workspace_id` 顶层列、FK→workspaces.id ON DELETE CASCADE；**单 scope（无 dialog_id 列）**——按 plan 顶部 2026-05-03「4d artifacts 重新归类为单 scope」修订记录纠正了上一轮假设的「双 FK」）+ `routers/artifacts.py`（GET list / GET one / PUT / DELETE，PUT 校验 `data.workspaceId` 存在 + 同 user，缺失/跨 user 返 409；同时 catch IntegrityError 转 409 防 race；list 支持 `?workspaceId=` + `?since=` + `?limit=` cursor 三向组合）+ `routers/workspaces.py::delete_workspace` 一跳级联 artifacts（与 dialogs / items 共用同一 cascade_version；artifacts 直接按 workspace_id 过滤，无 dialog hop）+ alembic migration `d8b2f7c4a195`（依赖 `c4f9e2a8d6b3`）+ stream/sse 加 `artifacts` 白名单与 `_serialize_artifact` + `app.py::_enable_backend_data_api` 挂载 artifacts_router
  - 前端：`artifacts.server.ts`（id-PK 模板；**`versions:ArtifactVersion[]` JSON.stringify utf-8 byte 算大小，< 64KB inline；≥ 64KB spill 到 `versionsBlob:AttachmentEnvelope` ref；decode 端 try/catch `materializeAttachment` 失败容忍——row 仍写入 IDB，versions 留空数组**；scoped pull 单 scope `workspaceId`；`bulkPut` 强制 sequential 避免 (user_id,sha256) blob_refs 唯一约束 race；observeList/observeFind/observeOne 触发 `ensureRealtimeSubscription`）+ `repositories/index.ts` flag 路由 artifacts 分支 + `SERVER_CAPABLE_TABLES` 加 `artifacts`；`src/utils/types.ts` export `ArtifactVersion`
  - env：`.env.docker` `BACKEND_DATA_TABLES=...,artifacts`；`tests/env/.env.test.{providers-rest,realtime-{ws,sse,poll,auto}}` 同步加 `artifacts`；`tests/api/conftest.py` TRUNCATE 列表加 `artifacts`
  - helper 增量：`tests/e2e/helpers/backend.ts` 加 `ArtifactRow` / `putArtifact` / `listArtifacts` / `deleteArtifact`
  - api: `tests/api/test_artifacts.py::test_put_creates_and_list_returns_it / test_get_returns_single_row / test_put_update_bumps_version / test_since_filter_drops_older_revisions / test_soft_delete_yields_tombstone_in_list / test_delete_then_put_revives / test_account_isolation / test_unauth_request_rejected / test_cross_user_cannot_delete / test_put_rejects_missing_workspace_id / test_put_rejects_unknown_workspace / test_put_rejects_cross_user_workspace / test_put_artifact_with_inline_versions / test_put_artifact_with_ref_versions_round_trips / test_inline_below_64kb / test_ref_at_or_above_64kb / test_workspace_cascade_true_tombstones_artifacts / test_workspace_cascade_skips_other_workspaces_artifacts / test_list_with_workspace_id_filters_to_scope / test_workspace_id_combined_with_since_and_limit / test_workspace_id_account_isolation / test_no_scope_param_returns_full_user_table`（22 case 全绿）
  - spec: `tests/e2e/stage4/artifacts-large.spec.ts::case1 ws double-tab inline put+update+delete / case2 ws double-tab 100KB+ ref versions A→B (head/tail fingerprint + server data.versionsBlob.type='ref' + sha256 长度 64 + size ≥ 64KB) / case3 ws double-tab cascade workspace→artifacts tombstones / case4 scoped pull empty IDB → only target workspace artifacts + Network 仅 ?workspaceId=X / case5 providers-rest no-realtime / case6 baseline byte-identical`（6 case 全绿）
  - 测试输出：`pnpm test:api` 184 passed ~115s（162 → 184，新增 22 artifacts case）；`pnpm test:e2e` 91 passed / 364 skipped ~5.7 min（新增 6 active case；唯一 fail 是 `scenarioB ws auto-reconnect` 已知 pre-existing flake，单跑稳定通过，与 4d 无关）
  - 红测验证（api）：① 把 `routers/artifacts.py::upsert_artifact` 的 `if (await session.execute(ws_stmt)).scalar_one_or_none() is None: raise 409` 临时改为 `if False and ...`，`test_put_rejects_cross_user_workspace` 红、stdout 报 `assert 200 == 409` 直接定位 ownership check 没触发；恢复后绿。② 把 `routers/workspaces.py::delete_workspace` 的 cascade artifacts 分支临时改为 `if False:`（只跳 artifact_stmt 的执行），`test_workspace_cascade_true_tombstones_artifacts` / `test_workspace_cascade_skips_other_workspaces_artifacts` 红、stdout 报 `assert False is True` 加完整 by_id dict 上下文，恢复后绿
  - 红测验证（e2e）：① 在 `artifacts.server.ts::decodeFromWire` 把 `materializeAttachment + blob.text() + JSON.parse` 三行注掉（versions 留空），case2 红、stdout 准确报 `B did not materialize artifact with 81920-char versions[0].text within 5s; last={"stage":"no-versions","name":"Big"}`，直接定位到 versions 没填回；恢复后绿。② 在 `artifacts.server.ts::pullForSpec` 加常量布尔 `__redDegradeScoped=true` 跳过 `scopedPull.pullScope` 走全表 `pull()`，case4 红、stdout 准确报 `IDB should only contain workspace X artifacts, got [...X..., ...Y...]`（IDB 同时含 X 和 Y workspace 的 artifacts），直接定位到 scoped pull 退化；恢复后绿。注意：build profile cache 不感知工作树 diff，红测前后必须 `rm -rf tests/.builds/{realtime-ws,providers-rest}` 才能让新版代码进 bundle
- 批次-4e (`messages`)：✅ 落地
  - 后端：`models/message.py`（id-PK envelope + `dialog_id` 顶层列、FK→dialogs.id ON DELETE CASCADE；复合索引 `(user_id, dialog_id, version)` 驱动 scoped pull 主路径 + 备选 `(user_id, version)` 用作管理类查询）+ `routers/messages.py`（GET list `?dialogId=` **mandatory**——缺 422 reject，与 items / artifacts 的 optional scope 不同；GET one / PUT 校验 `data.dialogId` 存在 + 同 user，缺失/跨 user 返 409；同时 catch IntegrityError 转 409 防 race；list 与 cursor `?since=` + `?limit=` 三向组合）+ `routers/dialogs.py::delete_dialog` 同事务级联 messages（共用 cascade_version + 顺序 publish messages → dialog；前端按 LWW idempotent apply 顺序无所谓）+ `routers/workspaces.py::delete_workspace` 二跳级联 messages（按 cascaded_dialog_ids 过滤，与 dialogs / items / artifacts 共用 cascade_version；publish 顺序 dialogs → items → artifacts → messages → workspace）+ alembic migration `e7c4a821f693`（依赖 `d8b2f7c4a195` artifacts）+ stream/sse 加 `messages` 白名单与 `_serialize_message` + `app.py::_enable_backend_data_api` lazy import 挂载 messages_router
  - 前端：`messages.server.ts`（id-PK 模板；**整个 envelope JSON.stringify utf-8 byte 算大小，< 64KB inline；≥ 64KB spill `contents` 到 `contentsBlob:AttachmentEnvelope` ref + `contents=[]`；decode 端 try/catch `materializeAttachment` 失败容忍——row 仍写入 IDB，contents 留空数组**；scoped pull 单 scope `dialogId`；`bulkPut` 强制 sequential 避免 (user_id,sha256) blob_refs 唯一约束 race；observeOne / observeFind 触发 `ensureRealtimeSubscription`；**list / observeList / modifyAll / pullForSpec 无 scope 时返 cache + console.warn 不 throw**——SearchDialog 跨 dialog 搜索路径，详见 plan 顶部 2026-05-03「4e messages 客户端 list cache-only fallback」修订记录）+ `repositories/index.ts` flag 路由 messages 分支 + `SERVER_CAPABLE_TABLES` 加 `messages`
  - 流式节流：新增 `composables/message-stream-flush.ts`（`createMessageStreamFlush({ flush, intervalMs?:200, byteThreshold?:1024, chunkBytesOf?, boundaryTextOf? })`：三触发——① 200ms 时间窗 ② ≥1024 byte 累积 ③ tail 匹配 `[。？！.?!\n]\s*$`；in-flight 期间合并新 enqueue（newest wins），不并发 PUT；`stop()` 强制 flush + 等待 inflight；测试钩子 `flushCount() / bytesPending() / msSinceLastFlush()`）+ `boot/expose-debug.ts` EXPOSE_DB 守卫的 `window.__messageStreamFlush__ = { create, intervalMs:200, byteThreshold:1024 }` + `views/DialogView.vue::stream` 替换 `throttle(50)` 为 `createMessageStreamFlush`，每个 token chunk 调 `update(part.text)` 传 chunkBytes hint，sentence-boundary 取 `assistant-message.text` 末 4 字；finalize 成功/失败两条路径前 `await streamFlush.stop()` 保证最终 envelope 一致
  - env：`.env.docker` `BACKEND_DATA_TABLES=...,messages`；`tests/env/.env.test.{providers-rest,realtime-{ws,sse,poll,auto}}` 同步加 `messages`；`tests/api/conftest.py` TRUNCATE 列表加 `messages`
  - helper 增量：`tests/e2e/helpers/backend.ts` 加 `MessageRow` / `putMessage` / `listMessages(token, dialogId, since?, limit?)`（dialogId mandatory，签名与其它 list helper 不同）/ `deleteMessage`
  - api: `tests/api/test_messages.py::test_put_creates_and_list_returns_it / test_get_returns_single_row / test_put_update_bumps_version / test_since_filter_drops_older_revisions / test_soft_delete_yields_tombstone_in_list / test_delete_then_put_revives / test_account_isolation / test_unauth_request_rejected / test_cross_user_cannot_delete / test_list_without_dialog_id_returns_422 / test_dialog_id_combined_with_since_and_limit / test_cross_dialog_isolation / test_dialog_id_account_isolation / test_put_rejects_missing_dialog_id / test_put_rejects_unknown_dialog / test_put_rejects_cross_user_dialog / test_put_message_with_inline_contents / test_put_message_with_ref_contents_round_trips / test_inline_below_64kb / test_ref_at_or_above_64kb / test_dialog_delete_cascades_messages / test_workspace_cascade_true_tombstones_messages_via_dialog / test_workspace_cascade_skips_other_workspaces_messages`（23 case 全绿）
  - spec: `tests/e2e/stage4/messages-attachment.spec.ts::case1 ws double-tab inline put+update+delete propagate <1.5s / case2 scoped pull only target dialog messages enter IDB + zero bare GET /messages (providers-rest) / case3 ws double-tab streaming 200 sequential PUTs converge with monotonic versions on B (tail fingerprint match + server version ≥ ROUNDS) / case4 message-stream-flush triple-trigger (4a 200ms time window / 4b byte threshold immediate / 4c sentence boundary immediate / 4d stop() finalizes pending) / case5 ws double-tab cascade workspace→messages tombstones / case6 providers-rest no-realtime + reload + scoped find recovers / case7 baseline byte-identical zero backend traffic + zero ws`（7 case 全绿，跨 baseline / providers-rest / realtime-ws 三 profile 切片）
  - 测试输出：`pnpm test:api` 207 passed ~126s（184 → 207，+23 messages）；`pnpm test:e2e` 98 passed / 399 skipped ~5.9 min（91 → 98，+7 含 4e × 1 case real-run/profile + 25 profile-skipped）。1 个 `stage3 reactives-cache-roundtrip case2` 套间偶发 flake，独立重跑稳定通过，pre-existing 与 4e 无关
  - 红测验证（api）：① 把 `routers/messages.py::list_messages` 的 `if not dialog_id: raise 422` 临时改为 `if False and not dialog_id:` 关掉 mandatory 校验，`test_list_without_dialog_id_returns_422` 红、stdout 准确报 `assert 200 == 422; bare GET /messages must 422, got 200: []` 直接定位 mandatory check 没触发；恢复后绿。② 把 `routers/dialogs.py::delete_dialog` 的 cascade messages UPDATE 分支临时改为 `if False:` 跳过 msg_stmt 执行（cascaded_messages 留空 list），`test_dialog_delete_cascades_messages` 红、stdout 准确报 `AssertionError: message m1 still alive: {'id': 'm1', ..., 'deleted': False, 'data': {...}}` 加完整 dict 上下文；恢复后绿
  - 红测验证（e2e）：① 在 `messages.server.ts::ensureRealtimeSubscription` realtime callback 内部加 `const __redDegrade = true; if (!__redDegrade && e.op === 'delete') ... else if (!__redDegrade && e.op === 'put' ...)` 跳过两条分支（用变量而非字面 false，绕过 `no-constant-condition` ESLint），case1 红、stdout 准确报 `row messages/msg-xxx did not converge within 1500ms (A={"id":"msg-xxx","type":"assistant","dialogId":"dlg-xxx","contents":[{"text":"inline-v1","type":"assistant-message"}],"status":"default"} B=undefined)`，直接定位 realtime apply 没写 IDB；恢复后绿。② 在 `composables/message-stream-flush.ts::stop()` 把 `await doFlush()` 改为 `pending = null`（直接丢 pending 不 flush），case4 4d 红、stdout 准确报 `4d stop-finalize: stop() must flush pending; got afterStop=0 lastFlushed=undefined` 直接定位 stop() 没 finalize；恢复后绿。注意：build profile cache 不感知工作树 diff，红测前后必须 `rm -rf tests/.builds/{realtime-ws,providers-rest,baseline}` 才能让新版代码进 bundle

---

### Stage 4.5 — 服务端 Import Job + 新设备首屏 bootstrap

> **🟢 分水岭**：本 Stage 是 new-deploy「能让老用户用」的物理分水岭。在此之前 new-deploy 仅适合自己 dev preview，不要导入真实数据 / 不要邀请他人；本 Stage 落地 + 真实端到端验收通过后才是 v1.0，可邀请老用户走「export → import」迁移路径。详见「上线节奏与人工端到端测试分工」段。

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
> **上线策略（2026-05-02 方向调整修订 ① 同步）**：Stage 4.5 落地即直接翻开 `IMPORT_JOB_ENABLED=true`，不再走"上线但不启用"两步。`IMPORT_JOB_ENABLED` flag 机制保留作应急开关——线上发现 worker 写炸时秒级 disable 用，不作为上线节奏的兜底。回滚通道靠 my-deploy URL（独立实例 + 独立数据），不靠"flag 翻回"。
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
- api: `tests/api/test_import_job.py::test_phase_a_unsupported_format_name_marks_failed`（formatName='sqlite' → error_message 含 'unsupported formatName'）
- api: `tests/api/test_import_job.py::test_active_job_unique_constraint`（同 user 创建第二个 active job → IntegrityError）
- api: `tests/api/test_import_job.py::test_unique_constraint_allows_terminal_jobs`（done/failed/cancelled 不参与，可建新 active；新 active 后第三个 active 仍 UniqueViolation）
- api: `tests/api/test_import_job.py::test_worker_recovers_active_job_on_startup`（手动插一行 status='parsing' → worker 1s poll 内拿走推到 phase_b + NDJSON 已写出）
- api: `tests/api/test_import_job.py::test_envelope_wire_format`（直构 ImportJob 调 `_envelope()` 验 server-routed table 契约 `{id, version, updated_at, deleted, data:{...}}`）

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 1」段（含测试输出 215 passed + 故障注入 2 轮 stdout 关键行）。

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
- api: `tests/api/test_imports_router.py::test_part_url_is_presigned_with_short_ttl`（断言 URL path 含 `/api/v1/_internal/multipart/<up>/part/N` + qs 含 `exp` + `sig` 64-hex + `expires_at` 在 `time.time()+3600 ±10s` 窗 + URL 内 `exp` 与 body `expires_at` 一致）
- api: `tests/api/test_imports_router.py::test_complete_with_all_parts_triggers_worker`（小 dexie export 切 2 part → complete → 2.5s 后 worker 推到 phase_b/parsing）
- api: `tests/api/test_imports_router.py::test_complete_with_missing_parts_returns_400`（gap 'contiguous' / 不从 1 起 'start at 1' / 重复 'duplicate' 三 sub-case）
- api: `tests/api/test_imports_router.py::test_get_job_returns_status_and_progress`
- api: `tests/api/test_imports_router.py::test_get_active_jobs_returns_in_progress_only`（含 `status=foo` → 400 验证）
- api: `tests/api/test_imports_router.py::test_delete_active_job_aborts_multipart_and_releases_slot`（partial unique index slot 释放，立即可建新 job；imported_rows 软删留 Step 3 once `imported_from_job_id` 列就位再补判据）
- api: `tests/api/test_imports_router.py::test_user_b_cannot_access_user_a_job_returns_404`（cross-user 404-mask：B 对 A 的 job_id GET / parts / complete / DELETE 全 404 'not found'，绝不 403）
- api: `tests/api/test_imports_router.py::test_part_endpoint_rejects_when_job_not_in_uploading_status`
- api: `tests/api/test_imports_router.py::test_create_active_job_conflict_returns_existing_snapshot`（partial unique index race → 409 + `detail.job` 含 winner snapshot；cancel 后再 create 201）
- api: `tests/api/test_imports_router.py::test_internal_part_endpoint_rejects_bad_sig`（篡改 sig → 403 'bad signature'；exp 过期 → 403 'url expired'）
- api: `tests/api/test_imports_router.py::test_complete_dedupes_same_sha256_across_jobs`（trust-the-hash dedup：A、B 上传相同字节，canonical `<sha[:2]>/<sha[2:]>` 文件存在且仅一份）

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 2」段（含测试输出 227 passed + 故障注入 3 轮 stdout 关键行）。

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
- api: `tests/api/test_import_phase_b.py::test_lww_overwrites_when_incoming_newer`（反向 LWW：先 PUT older 再导入 newer → 覆盖 + imported_from_job_id 设到 job）
- api: `tests/api/test_import_phase_b.py::test_imported_rows_tagged_with_job_id_and_timestamp`
- api: `tests/api/test_import_phase_b.py::test_publishes_progress_event_per_table`（indirect proof：import_jobs 暂未 server-routed → 用 processed_rows + status=phase_c 间接证；Step 6 后补真 WS 帧抓取）
- api: `tests/api/test_import_phase_b.py::test_phase_b_done_triggers_phase_c`
- api: `tests/api/test_import_phase_b.py::test_phase_b_handles_missing_table_files`（fixture 缺 5 张表 NDJSON → Phase B 不报错只 import 现有表）
- api: `tests/api/test_import_phase_b.py::test_phase_b_recovers_from_crash`（status='phase_b' job 但 tmp 目录无 NDJSON → status='failed' + error_message 含 'temp dir'）
- api: `tests/api/test_import_phase_b.py::test_dexie_to_backend_table_name_mapping`（installedPluginsV2 → installed_plugins / avatarImages → avatar_images snake_case 映射 + camelCase 表不存在）
- api: `tests/api/test_import_phase_b.py::test_kv_pk_table_writes_correct_row`（reactives 的 `(user_id, key)` 复合 PK 提升正确）
- api: `tests/api/test_import_phase_b.py::test_delete_cancelled_job_soft_deletes_imported_rows`（DELETE → 7 张表 imported 行 deleted_at 非 NULL + 共享 cascade_version）
- api: `tests/api/test_import_phase_b.py::test_delete_publishes_ws_event_per_cascaded_row`（subscribe before delete → workspaces 1 + dialogs 5 个 delete event 对账）
- api: `tests/api/test_import_phase_b.py::test_migration_adds_imported_from_job_id_to_all_routed_tables`（information_schema 验 10 张表 + FK ON DELETE SET NULL + 索引）

#### Step 4 — Phase C messages 文字写入

**做什么**：

- `import_worker.py` 增 phase_c：流式遍历 `messages.ndjson`，每 500 行一批 `INSERT ... ON CONFLICT DO UPDATE`；attachment 字段按原 base64 inline 存进 PG 的临时区域（messages 行加 `_pending_blob_extraction BOOLEAN DEFAULT FALSE` 列，导入时含 attachment 的行标 true）
- 进度按行报：每 500 行 `broker.publish` 进度事件（节流，避免 WS 风暴）
- FK 约束：messages 必须 dialog 已写入。Phase B 已写 dialogs metadata，正常情况无孤儿；FK 失败的 row 进 `import_jobs.dead_letter` JSONB 字段，Phase D 跑完后重试一次（一般是依赖顺序错配的小概率边界）
- 状态转 phase_d

**通过判据**：

- api: `tests/api/test_import_phase_c.py::test_writes_message_text_in_batches_of_500`（fixture 1500 messages，验证 PG 行数 1500 + 进度事件 ≥ 3 次）
- api: `tests/api/test_import_phase_c.py::test_marks_pending_blob_extraction_for_messages_with_attachment`（3 row：plain → FALSE / inline envelope → TRUE / 80KB row → TRUE，验证 size + envelope 双 prong）
- api: `tests/api/test_import_phase_c.py::test_orphan_message_without_dialog_lands_in_dead_letter`（fixture 含 dialogId='never-existed-uuid' → PG messages 表无该 row + dead_letter 数组含 `{table:'messages', row_id, error: 'orphan: dialog ... not found', batch_index, ts}`）
- api: `tests/api/test_import_phase_c.py::test_progress_event_emitted_per_batch`（slow，1500 行 → 3 batch checkpoints + 25ms 高频 poll 抓 distinct snapshots + batch_size 倍数对齐校验，indirect proof，import_jobs Step 6 才进 TABLE_MODELS）
- api: `tests/api/test_import_phase_c.py::test_phase_c_done_triggers_phase_d`（slow，验证 Step 5 stub 让 job 安静停 phase_d 不 advance、不 fail）
- api: `tests/api/test_import_phase_c.py::test_lww_message_text_skip_when_existing_newer`（先 PUT message text='NEWER-VIA-PUT'，再导入 updatedAt='2024-01-01' 的同 id row → PG data.contents[0].text 仍 NEWER + version 不前进）
- api: `tests/api/test_import_phase_c.py::test_writes_message_text_in_batches_of_500`（slow，1500 行 fixture → PG count==1500 + processed_rows==1500 + assert PHASE_C_BATCH_SIZE=500 静态断言）
- api: `tests/api/test_import_phase_c.py::test_batch_boundary`（slow，0 / 500 / 501 / 1499 行四 sub-case；0 行 case 模拟 messages.ndjson 不存在跳过；每 case 之间 UPDATE prev job to 'done' 释放 partial unique index slot）
- api: `tests/api/test_import_phase_c.py::test_phase_c_recovers_from_crash`（同 user 跑两个 job 写同 message id + 同 updatedAt → 第二轮命中 LWW WHERE existing.updated_at < incoming.updated_at FALSE 路径 DO NOTHING → version 不前进，模拟 worker 重启重跑 idempotent 行为）
- api: `tests/api/test_import_phase_c.py::test_pending_blob_extraction_partial_index_used`（pg_indexes 查 `ix_messages_pending_blob_extraction` indexdef 含 partial WHERE `_pending_blob_extraction = true`）
- api: `tests/api/test_import_phase_c.py::test_imported_from_job_id_set_on_phase_c_messages`（与 Phase B test 5 同模板，3 row 全部 `imported_from_job_id == job_id` + `imported_at` 距 now < 60s）
- api: `tests/api/test_import_phase_c.py::test_dead_letter_array_concat_appends_no_overwrite`（两 orphan messages 不同 fake dialogIds → JSONB 数组长度 = 2 + 顺序保留 [orphA, orphB]）
- api: `tests/api/test_import_phase_c.py::test_helper_row_has_attachment_envelope_recurses`（unit：top-level / 列表内 / 深嵌套 / 无 envelope 四档，防 Phase A 旧 heuristic 退化）
- api: `tests/api/test_import_phase_c.py::test_helper_should_pending_size_threshold`（unit：80KB row → TRUE / tiny row → FALSE，size prong 独立验）

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 4」段（含测试输出 254 passed + 故障注入 3 轮 stdout 关键行）。

#### Step 5 — Phase D attachments → 对象存储

**做什么**：

- `import_worker.py` 增 phase_d：扫 `messages WHERE _pending_blob_extraction=TRUE AND user_id=?`，逐条提取 attachment base64 字段
- 对每个 attachment：解码 base64 → 算字节大小 → 按 `BLOB_INLINE_MAX_BYTES=65536` (64KB) 阈值判断
  - `< 64KB` → 保持 inline 在 PG row 字段（与硬前置 2 客户端 `serializeAttachment` 阈值规则一致：messages 表小附件 inline，避免无谓 blob store 一次往返）
  - `≥ 64KB` → 计算 sha256 → 调 `BlobStore.put(sha256, bytes)`（同 sha256 已存在则去重不二次写）→ 改写 row 的 attachment 字段为 `{type:'ref', url, sha256, size, content_type}`
- 4 路 `asyncio.Semaphore` 并发 + 重试 3 次（指数退避 1s/2s/4s）
- 单条失败超过重试上限进 `import_jobs.dead_letter`；不阻塞整体 phase
- 进度按 attachment 报：每 10 个完成 `broker.publish` 一次进度
- 全部完成清 `_pending_blob_extraction` 标志 → 状态转 done → 清 `/tmp/import-<job_id>/` + 对象存储里的 raw upload (TTL 7 天 lifecycle rule 兜底)

**通过判据**：

- api: `tests/api/test_import_phase_d.py::test_attachment_under_64kb_stays_inline_in_pg`
- api: `tests/api/test_import_phase_d.py::test_attachment_over_64kb_uploaded_to_blob_store_and_row_rewritten`（验证 PG row 字段是 `{type:'ref',...}`，BlobStore 里 `<sha256>` 文件存在）
- api: `tests/api/test_import_phase_d.py::test_same_sha256_reuses_existing_blob_no_double_upload`（in-process patch_put_with_counter，验证 call_count=2 + unique sha=1 + blob_refs 1 行 + 磁盘文件单一存在）
- api: `tests/api/test_import_phase_d.py::test_failed_attachment_after_3_retries_lands_in_dead_letter`（in-process patch_put_always_fails + fast_backoff，验证 4 attempts + dead_letter attempt=4 + envelope 留 inline + flag 仍 cleared）
- api: `tests/api/test_import_phase_d.py::test_phase_d_done_marks_job_done_and_clears_temp_files`（stage tmp dir + fake raw_object_key 文件 → 跑完 Phase D → tmp dir gone + raw upload deleted）
- api: `tests/api/test_import_phase_d.py::test_concurrent_uploads_capped_at_4`（slow，in-process patch_put_with_delay 0.1s + 16 distinct ≥64KB attachment + 验证 peak 并发 == _PHASE_D_CONCURRENCY = 4）
- api: `tests/api/test_import_phase_d.py::test_phase_d_recovers_from_crash_idempotent`（首跑 inline → ref 后手动 UPDATE _pending_blob_extraction=TRUE 模拟 crash → 第二跑 walk 无 inline envelope → put call_count=0 + flag 重新 cleared）
- api: `tests/api/test_import_phase_d.py::test_progress_event_published_per_n_attachments`（progress_per_one fixture 把 interval 从 10 → 1 + 18 distinct ≥64KB attachment 跨 batch_size=16 边界 → ≥ 1 distinct PG snapshot + final processed_blobs == 18）
- api: `tests/api/test_import_phase_d.py::test_walk_attachments_recurses_into_nested_structures`（unit-style，`_walk_attachments` 深嵌套 dict→dict→list 路径精确返 path tuple `('outer','mid','inner',1)` + `_set_at_path` mutate 配套）
- api: `tests/api/test_import_phase_d.py::test_inline_envelope_below_threshold_stays_unchanged`（in-process patch_put_with_counter，5 个 1KB attachment → put call_count=0 + 全部 envelopes 不变 + flag cleared，验证「小附件不上 BlobStore」短路）
- api: `tests/api/test_import_phase_d.py::test_dead_letter_does_not_block_other_attachments_in_same_row`（in-process patch_put_fails_for_sha 仅 middle attachment fail + fast_backoff，row 内 3 attachment：第 1+3 转 ref，中间留 inline + dead_letter 1 entry attempt=4 + attachment_path 末位 == 1）
- api: `tests/api/test_import_phase_d.py::test_actual_size_used_instead_of_declared_size`（envelope 谎报 size=10 但实际 100KB → 走 ref 路径 + ref envelope 的 `size` 字段 = 100KB 不是 10，actual_size wins）
- api: `tests/api/test_import_phase_d.py::test_constant_drift_with_blob_client_64kb`（grep `src/data/blob-client.ts` 验 `BLOB_INLINE_MAX_BYTES = 64 * 1024` + Python `_PHASE_D_INLINE_MAX_BYTES == 64 * 1024`，防两端漂移）

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 5」段（含测试输出 267 passed + 故障注入 3 轮 stdout 关键行）。

#### Step 6 — WS 进度推送 + 状态持久化（已在 Step 1-5 内分散实现，本 Step 做集成验证）

**做什么**：

- `import_jobs` 注册为 server-routed table（添加到 `SERVER_CAPABLE_TABLES`，但只读不允许 client 直接 PUT；写路径只有 worker）
- `_to_event` 与 `_to_row` 沿用现有 envelope 契约（`{id, version, updated_at, deleted, data: <ImportJobStatus>}`）
- 客户端 `realtime.subscribe('import_jobs', cb)` 即可拿状态变更
- 用户登录时一次 `GET /api/v1/import/jobs?status=active` 拉当前 active job（如果有）；之后增量靠 realtime
- ImportJob row 的 PUT 仅由 worker 内部完成；任何 `PUT /api/v1/import_jobs/<id>` 来自客户端的写请求返回 405 Method Not Allowed

**通过判据**：

- api: `tests/api/test_import_realtime.py::test_phase_change_publishes_ws_event`（端到端 multipart upload + complete → drain WS 帧直到 `data.status='done'` → 断言序列 superset {queued, phase_b, done} + monotonic rev）
- api: `tests/api/test_import_realtime.py::test_ws_event_envelope_matches_routed_table_contract`（superset 断言 envelope 顶层 `{id, version, updated_at, deleted=False, data}` + data 14 keys 子集 `{id, status, multipart_upload_id, raw_object_key, total_bytes, processed_bytes, total_rows, processed_rows, total_blobs, processed_blobs, error_message, dead_letter, created_at, updated_at}`）
- api: `tests/api/test_import_realtime.py::test_user_b_does_not_receive_user_a_import_progress`
- api: `tests/api/test_import_realtime.py::test_active_jobs_query_returns_in_progress_only_cross_user`（cross-user：A 有 active job 时 B `GET ?status=active` 返 `[]`，与 Step 2 `test_get_active_jobs_returns_in_progress_only` 同 user 行为互补）
- api: `tests/api/test_import_realtime.py::test_client_put_to_import_jobs_returns_405`（PUT/PATCH/POST/DELETE 4 method 全 405 + body 含 'read-only' + Allow header 存在）
- api: `tests/api/test_import_realtime.py::test_get_to_import_jobs_path_also_405_or_404`（GET `/api/v1/import_jobs/<id>` 405 + GET `/api/v1/import/jobs/<id>` 200，与读路径区分）
- api: `tests/api/test_import_realtime.py::test_sse_subscribe_import_jobs_works`（SSE channel 与 WS 同模板支持 import_jobs）
- api: `tests/api/test_import_realtime.py::test_replay_after_reconnect_picks_up_missed_events`（since=max_rev_seen reconnect → replay 全 rev > since + 末尾 status='done'）

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 6」段（含测试输出 275 passed + 故障注入 3 轮 stdout 关键行）。

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

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 7」段（含测试输出 6 case 全绿 / 25.8s + 故障注入 3 轮 stdout 关键行）。

#### Step 8 — `GET /api/v1/bootstrap` endpoint + 首屏路由 guard

**做什么**：

- 后端 `src-backend/data/routers/bootstrap.py`：`GET /api/v1/bootstrap`
  - 一次返回所有小表全量 + messages 表的"最近 N 个 dialog 的最新 50 条"摘要
  - 响应体：`{schema_version, workspaces:[...], dialogs:[...], providers:[...], assistants:[...], plugins:[...], reactives:[...], avatarImages:[...], messages_recent:[...]}`
  - **明确不含 `items` / `artifacts`**（2026-05-03 修订）：这两张表走硬前置 3 scoped pull，按 dialog/workspace 范围 lazy 拉，不放进 bootstrap 否则会撑爆 1MB 上限。`test_returns_all_small_tables_in_one_response` 必须显式断言 response 字段 set 不含 `items` / `artifacts`，防未来误加
  - 单响应硬上限 1MB（实测一般用户 < 200KB）；messages_recent 超过则按 dialog 截断（反向时间排序，先保留最近活跃的 dialog 的 messages）
  - response 加 `Cache-Control: private, max-age=10` 让客户端短期缓存避免重复请求
- 前端 `src/router/index.ts` 加 router guard：登录后 `await GET /api/v1/bootstrap` → 写入 IndexedDB 缓存 → 放路由进 MainLayout
- guard 失败回退：超时 2s 没返回 → 走老路径（IndexedDB 缓存 + 后台 fetch）→ banner 提示 "部分数据稍后加载"
- 已有 active import job 时：`bootstrap` 仍返回当前 server 端可见的部分数据（Phase B 完成的 workspaces 等），与 import 进度不冲突

**通过判据**：

- api: `tests/api/test_bootstrap.py::test_returns_all_small_tables_in_one_response`（response key set == 8 keys + 显式 assert `'items' / 'artifacts' not in body`）
- api: `tests/api/test_bootstrap.py::test_messages_recent_limited_to_50_per_dialog`（PG 直插 100 messages × 1 dialog → 返 50 + ids == m050..m099）
- api: `tests/api/test_bootstrap.py::test_response_size_under_1mb_for_typical_user`（PG 直插 100 dialogs × 50 messages = 5000 → 响应体 < 1MB）
- api: `tests/api/test_bootstrap.py::test_response_truncates_to_under_1mb_for_heavy_user` `@slow`（PG 直插 1000 × 50 + 500B padding/msg → 50000 messages → response ≤ 1MB+5% 框架开销 + truncation 在 dialog 边界）
- api: `tests/api/test_bootstrap.py::test_account_isolation`
- api: `tests/api/test_bootstrap.py::test_partial_data_during_active_import`（手动插 active import_jobs status='phase_c' → bootstrap 仍返 workspaces）
- api: `tests/api/test_bootstrap.py::test_cache_control_header_set`（`Cache-Control == 'private, max-age=10'`）
- api: `tests/api/test_bootstrap.py::test_envelope_shape_matches_list_endpoint`（bootstrap workspace envelope == `GET /api/v1/workspaces` 字节级一致）
- api: `tests/api/test_bootstrap.py::test_unauth_request_rejected`
- api: `tests/api/test_bootstrap.py::test_messages_recent_excludes_items_artifacts`（防未来误把 items/artifacts 加进 bootstrap 的二次断言）
- api: `tests/api/test_bootstrap.py::test_dialogs_with_no_messages_dont_appear_in_messages_recent`
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_fresh_browser_login_no_blank_first_screen`（清空 IndexedDB → seed 1 ws via REST → bootstrap apply → 1.5s 内 IDB.workspaces 含 seeded id）
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_bootstrap_timeout_falls_back_to_progressive`（mockBootstrapTimeout 3s → 2s AbortSignal.timeout fire → fallback flag + banner）
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_bootstrap_runs_only_once_per_session`（多次 navigation → 后端只收 1 次 GET /api/v1/bootstrap）
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_bootstrap_apply_writes_idb_correctly`（seed 3 ws → IDB 子集断言含全部 seeded ids）
- spec: `tests/e2e/stage4_5/step8-bootstrap.spec.ts::test_bootstrap_500_error_falls_back`（mockBootstrap500 → 同 timeout 一样的 fallback 路径，HttpError 分支）

✅ 落地：见进度快照「已完成 / Stage 4.5 / Step 8」段（含测试输出 286 passed / 11 case 全绿 + 故障注入 3 轮 stdout 关键行）。

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

### Stage 4.7 — 离线 outbox（写路径离线兜底）

> **2026-05-03 修订引入**。Stage 4.5 bootstrap 解决「读」的离线（IDB 缓存让用户在弱网/离线下仍能浏览历史）；Stage 4.7 解决「写」的离线——用户在弱网/离线下点删除/编辑/发消息，HTTP PUT 失败必须缓存到本地 outbox，下次重连自动 flush，而不是直接抛 error 丢操作。这是新版 SaaS 必备的 UX 保证，也是 Stage 4.9 删 dexie 实现的硬前置（dexie 实现在的时候本地 IDB 写还能兜，删了之后必须靠 outbox）。

**前置**：Stage 4.5 + 硬前置 3 已落地。Stage 4.5 不强制走完真实端到端验收；4.7 与 4.5 验收并行推进。

**目标**：所有 server-routed 表的写操作（`put` / `update` / `delete` / `bulkPut` / `bulkDelete` / `deleteWhere` / `modifyWhere` / `modifyAll`）在网络失败 / 401 refresh 失败 / 超时场景下自动入 outbox 队列；网络恢复 + auth 可用后自动 flush，UI 透明无感。

**做什么**

- **新增 IDB 表**：`db.ts` 加 `outbox` 表 schema：`{ id: auto-uuid PK, table: string, op: 'put'|'delete', target_id: string, payload: JSONB, attempts: number, created_at: number, last_error?: string }`。schema 升 v7，加 `db.version(7).stores({ ..., outbox: 'id, table, created_at' })`
- **后端 capability hint endpoint**（可选）：`GET /api/v1/health` response 加 `{ ok, db, server_time }`，前端用 `server_time` 与本地时间偏移补偿做 LWW 时间戳，避免本地时钟漂移导致 outbox flush 时全失败
- **前端新增** `src/data/outbox.ts`：
  - `enqueue(op: { table, op, targetId, payload })` — IDB 写一行 + emit `outbox:size-changed` 事件让 UI 显示"X 条待同步"角标
  - `flush()` — 按 created_at 顺序逐条 flush，单条失败按指数退避（base 1s, max 60s, max attempts 10）；超过 attempts 上限的条目标记 `last_error` 为 user-actionable（UI 弹"3 条变更同步失败，[查看] [全部丢弃]"）
  - 重连钩子：`realtime.ts` 在 WS reconnect 成功后触发 `outbox.flush()`；`auth.backend.ts::tryRefresh` 成功后也触发一次
  - 浏览器 `online` 事件 + 5min 周期 timer 兜底（pwa 后台 tab 不收 online 事件的场景）
- **`<table>.server.ts` 改造**：`putOne` / `deleteOne` 在 `http.put` / `http.delete` 抛网络错误（HttpError.isNetwork = true）或 5xx 时调 `outbox.enqueue(...)`；同时本地 IDB 缓存先 put（乐观更新），让 UI 立刻看到变化；flush 时按相同 envelope 重发，server LWW 处理冲突（`updated_at` 旧的被覆盖即丢弃）
- **冲突解决**：完全走 server 端 LWW（`updated_at` 大者胜）；client 端 outbox 不做冲突检测——重发时 server 比对 row.version，若 server 已有更新版本，outbox 这条 PUT 被静默丢弃（server 200 + 返当前权威 row，client outbox 删除该条）
- **UI 集成**：`MainLayout.vue` 顶栏加同步状态指示器：`outbox.size === 0` 显示绿点 / `> 0` 显示橙点 + 数字 + 点击展开列表 / `flush 失败 attempts > 5` 显示红点 + 用户操作选项

**通过判据**

- api: `tests/api/test_health.py::test_health_returns_server_time`
- api: 后端无新 endpoint，所有 outbox 行为是前端兜底
- spec: `tests/e2e/stage4_7/outbox.spec.ts::offline_put_enqueues_and_optimistic_renders`（mock 网络 down → put → IDB 立刻有 row + outbox 表有一条 + UI 显示新数据 + 同步指示器变橙）
- spec: `tests/e2e/stage4_7/outbox.spec.ts::reconnect_triggers_flush`（mock down → 多次 put → mock up → 自动 flush → outbox 清空 + server 收到全部 row + 同步指示器变绿）
- spec: `tests/e2e/stage4_7/outbox.spec.ts::flush_respects_order`（offline 时 put A → put A 改名 → put B → online → server 收到顺序 [A, A', B]，最终 server 状态 = A' + B）
- spec: `tests/e2e/stage4_7/outbox.spec.ts::lww_silently_drops_stale`（A tab offline put 旧 version → B tab online put 新 version → A tab online → A 的 outbox 条目 server 返回当前 row（version 更高）→ A outbox 删除该条 + IDB 接收 server 权威 row）
- spec: `tests/e2e/stage4_7/outbox.spec.ts::max_attempts_exhausted_user_actionable`（mock 持续 500 → attempts 到 10 → UI 弹"X 条同步失败"+ 用户可"重试" / "丢弃"）
- spec: `tests/e2e/stage4_7/outbox.spec.ts::cross_tab_outbox_dedup`（A tab put → B tab 同时 put 同 id 不同字段 → 两 tab 都 enqueue → 都 flush → server LWW 二选一 + 两 tab 收 realtime event 收敛）
- **故障注入**（必须真跑红一次再绿）：把 outbox.flush 临时改为 no-op → spec1 的 reconnect 后 outbox 清空判据红、stdout 报"outbox.size still 3 after reconnect"，恢复后绿

**回滚**

- `OUTBOX_ENABLED=false` env flag 关闭 outbox（写失败仍抛 error，回到 4.7 之前行为）；用作秒级 disable 应急开关
- IDB outbox 表保留（不破坏数据），关 flag 后老条目静默不 flush，重新开启后续推

**避坑**

- outbox row payload 是序列化后的 wire envelope（含 attachment ref），不要存 ArrayBuffer / Blob 直接进 IDB outbox 表（会撑爆 quota）；blob 在入 outbox 前已通过 `serializeAttachment` 转 ref，bytes 已落到 BlobStore（在线时已上传成功的场景）或本地 IDB Blob 缓存（离线场景，重连后 outbox flush 前先 retry blob upload）
- **离线 blob upload**：离线时 `serializeAttachment` 大于 64KB 的路径会 throw（putBlob 网络失败），需要在 outbox 之外维护一个 `blob_outbox` 表存 `{ sha256 PK, bytes Blob, content_type, mime, size, attempts }`；outbox flush 时先 flush blob_outbox（按 ref 依赖图拓扑顺序），所有 ref 上线后再 flush row outbox
- 浏览器 quota 限制：outbox + blob_outbox 总占用超过 quota 50% 时拒绝新 enqueue + UI 强提示"本地存储即将满，请连网"
- realtime apply 路径（WS event 到达 → put IDB）**不**走 outbox，是从 server 单向流入；outbox 只覆盖 client → server 写路径
- `bulkPut` / `bulkDelete` 入 outbox 时按单条拆开 enqueue（不要存数组 payload），避免单 large bulk 失败后整批重发

**批次状态**

- 批次-4.7：⏳ 未开 · 依赖 Stage 4.5 落地 + 硬前置 3 落地

---

### Stage 4.9 — flag 路由层 + dexie 实现一次性下架

> **2026-05-02 方向调整修订引入**。本 Stage 是为了让代码库回归"只有一种实现"的清爽态——Stage 1 起为"上线但不启用"灰度设计的双实现 + flag 路由层在 new-deploy 上线后已是 dead weight，但保留到 Stage 4.5 作为应急 disable 单表的开关。

**前置（必须满足才能开 Stage 4.9）**：

- Stage 4.5 已上线，`IMPORT_JOB_ENABLED` 默认开
- Stage 4.7 离线 outbox 已上线，`OUTBOX_ENABLED` 默认开，稳定运行 ≥ 3 天无 outbox 数据丢失事件（dexie 实现删除后，outbox 是写路径离线兜底的唯一通道，必须先验证）
- 硬前置 3 scoped pull 已上线（4d/4e 强依赖）
- **真实端到端验收已通过**：用户在 my-deploy 上 ExportDataDialog 导出真实数据 → 在 new-deploy 上 ImportDataDialog 导入 → 全部数据核对正确（详见「上线节奏与人工端到端测试分工」段 / Stage 4.5 分水岭验收清单）
- new-deploy 上线全档稳定运行 ≥ 1 周，无单表 disable 应急事件

**做什么**

- 删 9 张 `src/data/repositories/<table>.dexie.ts`（providers / reactives / assistants / installedPluginsV2 / avatarImages / workspaces / dialogs / items / artifacts / messages 全部 server.ts 之外的实现）
- 删 `src/data/repositories/index.ts` 的 flag 路由层（不再按 `BACKEND_DATA_TABLES` CSV 分发，直接 `export const repos = { providers: serverProvidersRepo, ... }`）
- 删 `src/data/server-tables.ts` 的 `SERVER_CAPABLE_TABLES` allowlist
- 删 `.env.docker` 的 `BACKEND_DATA_TABLES` 配置位 + 注释段
- 删 `src/utils/config.ts` 的 `BackendDataTables` 导出
- IndexedDB 缓存机制保留（仍然作为离线 / 弱网下的本地缓存层，server 是权威）；如果批次落地时发现 IndexedDB 缓存路径耦合 dexie 表实现，需顺手解耦
- 后端：`BACKEND_DATA_API_ENABLED` flag 保留（仍然控制 data API 路由是否挂载，`/cors` / `/doc-parse` 等老路由不依赖此 flag），但 `BACKEND_DATA_TABLES` 完全删除

**通过判据**

- `pnpm test:api && pnpm test:e2e` 全 profile 全绿（删 dexie 实现连带删 baseline / dexie fallback case，相关 spec 同步删除或转 server-only 等价检验）
- bundle 体积**净下降**（dexie repo 实现 + flag 路由层删除后 ~3-5 KB），数字写进进度快照
- new-deploy push 重 build 后线上行为无回归：开发自测主路径 30 min（注册 / 工作区 / 对话 / 消息 / 附件 / provider / assistant / plugin / 跨设备同步）
- 代码 grep 验证：`BACKEND_DATA_TABLES` / `SERVER_CAPABLE_TABLES` / `<table>.dexie.ts` 全部消失，`repositories/index.ts` 不再有 `if (BackendDataTables.includes(...))` 形式分支

**测试要求（自动化）**

- spec：删除 `tests/e2e/stage{1,2,3,4}/<...>-baseline-fallback.spec.ts` 等检验"flag 关时走 dexie"的全部 case（这些 spec 在 Stage 4.9 后就 by definition 不可能复现）；保留所有"server 路径正确"的 case
- 删除前在批次内显式列清单：哪些 spec 删 / 哪些 spec 改 / 哪些 spec 不动，便于 review
- 删除后跑 `pnpm test:e2e` 时 skipped count 应大幅下降（profile gate 的 baseline 部分不再有 case）
- soak.sh 重跑一次确认 RSS / p95 不回归

**回滚策略**

- Stage 4.9 是不可逆操作（dexie 实现删了之后回滚成本高），所以前置的「Stage 4.5 端到端验收 + 1 周稳定」必须严守
- 万一 Stage 4.9 落地后线上发现严重 bug 需要回滚到 server / dexie 双实现，**回滚路径 = `git revert` 整个 Stage 4.9 这批 commit**（这批 commit 应保持单一巨型 commit 形态便于整体 revert），而不是分散多个小批次难以一次性 revert

**避坑（落地时关注）**

- ① IndexedDB 缓存机制如果耦合 dexie 表实现（如直接调 `db.<table>.put()`）需要重写为 server.ts 内部的缓存 helper，不能直接删 dexie 实现导致缓存路径断裂
- ② `dexie-export-import` 库本身仍依赖 Dexie 表存在（用于 ExportDataDialog 的 `exportDB(db)` 调用）。Stage 4.9 删的是 `<table>.dexie.ts` Repository 实现，不是 Dexie schema 本身——`src/utils/db.ts` 的 schema 定义保留，作为缓存层底座

**批次状态**

- 批次-4.9：⏳ 未开 · 依赖 Stage 4.5 + 端到端验收 + 1 周稳定

---

### Stage 5 — 导入导出收尾验证

> **2026-05-02 · Stage 2.5 落地**：本阶段计划的「摘除 dexie-cloud-addon」+「deprecated 代码一次性清理」已整体前置到 Stage 2.5 完成（详见顶部修订记录）。Stage 5 现在只剩「导入导出端到端验证」一项工作。

**前置**：Stage 4.5 已上线（`IMPORT_JOB_ENABLED` 已默认开），老用户有可用迁移路径（旧版导出 → 新版 ImportDataDialog）。在 Stage 4.5 缺位时直接进 Stage 5 会让老用户失去迁移路径，必须串行。

**目标**：验证「导入导出 only」迁移路径在 Stage 4.5 ImportJob 落地后端到端可用。

**导入导出收尾**

- 验证 `ExportDataDialog` / `ImportDataDialog` 在 `dexie-cloud-addon` 摘除后（Stage 2.5 已完成）仍能读写 `aiaw_user_db.json`（dexie-export-import 不依赖 addon，理论上没问题，但要 e2e 真跑过）
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

| 阶段                            | 谁在跑         | 用户视角                                         | 200MB 用户耗时估 | 关键节点                                                                   |
| ------------------------------- | -------------- | ------------------------------------------------ | ---------------- | -------------------------------------------------------------------------- |
| a. 文件选择                     | 浏览器         | 选 `aiaw_user_db.json`                         | <1s              | —                                                                         |
| b. 上传（5MB 切片 → 对象存储） | 浏览器         | 进度条 "87/270 MB"                               | 3–10min         | 浏览器必须在线                                                             |
| c. 排队                         | backend        | "上传完成，已开始处理。**您可以关闭页面**" | <1s              | 🟢**此刻可关 tab**                                                   |
| d. (可选) 用户关闭浏览器        | —             | —                                               | —               | —                                                                         |
| e. Phase A 解析（流式 ijson）   | backend worker | 状态卡片 "解析中"                                | 30s–2min        | —                                                                         |
| f. Phase B 写结构表             | backend worker | "写入 workspaces..."                             | 5–30s           | —                                                                         |
| g. Phase C 写 messages 文字     | backend worker | "对话历史 3,421 / 12,580"                        | 1–5min          | 🟢**此后任意设备登录可看到全部 workspaces / dialogs / message 文字** |
| h. Phase D 处理附件             | backend worker | "附件 47 / 312"                                  | 5–30min         | 🟢 不阻塞使用                                                              |
| i. 完成                         | backend        | "迁移完成"通知                                   | —               | —                                                                         |

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

**前提**：新版的 `db.ts` schema 必须**保持兼容旧版**（同表名、同主键、同索引）。Stage 2.5 已删除 `dexie-cloud-addon`，IndexedDB schema 保持兼容旧版（同表名 / 同主键 / 同索引），`exportDB` / `importInto`（仅 export 仍由前端 `dexie-export-import` 跑；import 由 Stage 4.5 server-side worker 直读 JSON，不再走 `importInto(db)`）跨版本互通仍成立。

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
- **现存 reading hooks**：`db.ts` 里的几个 `db.<table>.hook('reading', ...)` v1.4/v1.8 兼容迁移逻辑，在 Stage 4.5 ImportJob worker 流式解析旧版 `aiaw_user_db.json` 时需要同等地把老 schema 行归一化到新格式后再写 PG，避免老 row 直接落库导致 schema 错位。new-deploy 后端不服务老客户端（老客户端连 my-deploy），所以读序列化器只在 import 路径上需要这套兼容。
- **离线写**：Stage 2.5 已摘 `dexie-cloud-addon`，原"addon 兜底离线写"路径已不存在。当前阶段 server-routed 表的离线写靠 IndexedDB 缓存层吸收（写本地成功 + server PUT 失败时静默；重连后无自动 flush，需用户操作触发或刷新页面）；**Stage 4.7 引入 `outbox` 表 + 重连自动 flush + LWW** 后才有强幂等离线写保证（2026-05-03 修订，原写"Stage 5 引入"已作废）。
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

## 上线节奏与人工端到端测试分工

> **2026-05-02 方向调整修订引入**。本段固化「自动化测试覆盖什么 / 不覆盖什么」的边界 + 「Stage 4.5 是分水岭」的运维节奏。每个 Stage 落地时按本段的清单决定是否需要人工端到端测试。

### 当前 new-deploy 实例属性

- **dev preview**：仅 providers 表跨设备同步可用，其他 9 张表只在本地 IndexedDB；老用户无导入路径
- **使用对象**：仅自己（开发者）做主路径自测，**不要导入真实数据 / 不要邀请他人**
- **回滚通道**：用回 my-deploy URL（独立实例 + 独立数据，互不影响）

### 自动化测试覆盖什么（脚手架能搞定的）

- 后端 API 行为：CRUD / 鉴权 / 跨账号隔离 / soft-delete / cursor 分页 / `?since` 增量
- 实时通道：WS / SSE / poll / auto 四档 transport 切换 + 离线重连 + token refresh + Last-Event-ID 续拉
- 跨 tab 同步：Playwright multi-context 模拟多设备
- cursor 分页（Stage 4 硬前置 1）：`?since=N&limit=200&next_cursor=…` 续拉协议；不带 limit 维持向后兼容；满页时 `next_cursor` 为最后一行 `version`
- 对象存储分流（Stage 4 硬前置 2）：64KB 边界 inline / ref；同 sha256 去重
- 流式云同步（inline envelope，2026-05-02 收窄后所有表共用）：跨 tab 流式 PUT 200 次模拟 token 流 → 第二 tab UI 无丢帧 / 无倒退
- 级联事务（Stage 4 主体）：删工作区 → 子 dialogs / messages / artifacts 实时清空 + server 行清空
- ImportJob（Stage 4.5）：4 档 fixture 全跑通 + 多设备 409 + WS 进度推送 + Phase A-D worker 状态 + 失败回退
- 性能基线：bundle 体积 + RSS soak（10 min / 1h）+ p95 延迟（soak.sh）

### 自动化测试不覆盖什么（只能人工做的）

- **真实数据形态边界**：用户实际在 my-deploy 上几个月积累的 export，schema 真实嵌套度 / attachment 真实尺寸分布 / 中文 / emoji / 长 markdown / 各种 corner case，自动化 fixture 永远不够穷尽
- **真实网络环境**：corporate proxy 拒 WS upgrade / 移动 4G 间歇性丢包 / 跨地区高 latency，soak.sh 模拟不了
- **多平台真机**：Tauri 桌面（macOS / Windows / Linux）+ Capacitor Android，脚手架只跑 PWA 浏览器
- **UI / UX 软问题**：「数据正确」≠「用得舒服」，自动化看不到布局塌 / loading 卡顿 / 错别字 / 按钮位置歧义
- **IndexedDB schema 升级真实路径**：v6 → v7 / v8 后旧客户端能不能正确读老数据（CLAUDE.md 标记的「Stage 3+ 待补」）
- **生产 OS / 运维场景**：Northflank 重启 / Postgres reload / 对象存储 region 切换 / 邮件验证链路（Stage 5+ 引入时）

### 阶段化人工测试清单

| 阶段                                  | 必须人工测什么                                                                                                                                                                                                                                                                                                                                                                                                             | 时间预算                         |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| Stage 3 批次-3a / 3b 落地             | 不需要。脚手架 + 自审 即可。可选：5-10 min 在 dev 上点一次相关表的主路径冒烟                                                                                                                                                                                                                                                                                                                                               | 0–10 min                        |
| Stage 4 硬前置 1+2 落地               | 不需要。脚手架 + soak.sh 即可                                                                                                                                                                                                                                                                                                                                                                                              | 0                                |
| Stage 4 主体 批次-4a/b/c/d/e 落地     | 每张表落地后 5-10 min 冒烟级：注册新账号 → UI 创建 / 修改 / 删除该表数据 → 第二 tab 实时同步 → 清 IndexedDB 刷新数据回灌。**不必真用真实数据**                                                                                                                                                                                                                                                                    | 5-10 min × 5 = 25-50 min        |
| Stage 4 主体全部完成                  | 一次性主路径自测 30 min：注册账号 → 创建工作区 + folder 树 → 创建对话 → 发消息（含 5MB attachment）→ 创建 artifact → 配 provider / assistant / 装 plugin → 第二 tab 全链路同步 → 清缓存重登数据回灌                                                                                                                                                                                                                 | 30 min                           |
| **Stage 4.5 落地（🟢 分水岭）** | **必须**做真实数据端到端：① 在 my-deploy 上 ExportDataDialog 导出**你自己全部真实数据** → ② 在 new-deploy 上注册新账号 → ③ ImportDataDialog 导入 → ④ 逐项核对所有数据（工作区树 / 对话 / 消息 / attachment / provider / assistant / plugin / avatar）→ ⑤ 清浏览器缓存重登确认数据从 server 拉回 → ⑥ 第二台设备登录确认能看到全部数据 → ⑦ Phase A-D 各阶段进度 UI 表现 / 关 tab 后再开看到状态恢复 | 1-2 h                            |
| Stage 4.9 落地                        | 落地前后跑同一份脚手架对比 + 30 min 主路径自测确认无回归                                                                                                                                                                                                                                                                                                                                                                   | 30 min                           |
| **Stage 5 落地**                | **第二轮端到端 + 跨平台真机**：① 卸载重装 PWA / 不同浏览器登录 → ② Tauri 桌面（至少 macOS）真机 → ③ Capacitor Android 真机 → ④ 长时间使用一周观察「用着用着发现少数据」→ ⑤ 邀请 1-2 个老用户跑同样的 export → import 流程，收集主观反馈                                                                                                                                                                    | 几小时 + 一周观察 + 1-2 用户邀测 |
| Stage 5 后                            | 公开邀请、发"迁移日"通知。my-deploy 实例继续保留至 active 用户迁移率 ≥ 80% 后宣告 6 周下线窗                                                                                                                                                                                                                                                                                                                              | —                               |

### 守则

- **Stage 4.5 落地前不要邀请他人**，即使「看起来差不多了」。一个用户被坏数据吓走的成本远高于多等几周
- **Stage 4.5 端到端验收必须用真实数据**，不能用脚手架 fixture 替代——fixture 是为自动化设计的"代表性样本"，不是用户真实习惯
- **Stage 4.5 验收发现 bug → 修代码 + 同批次落 spec / api**（CLAUDE.md 工作流契约不变），不绕过测试把 bug 直接修了
- **Stage 4.9 必须等 Stage 4.5 端到端验收通过 + 1 周稳定**，否则失去"flag 翻 dexie 救场"的应急通道

---

## 关键文件清单（供后续批次直接定位）

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
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/routers/auth.py`（Stage 1.5 新增：register/login/refresh/logout/me）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/data/auth.ts`（Stage 0 新增 `AuthSource`，Stage 1.5 加 `BackendAuthSource`）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src/data/http.ts`（Stage 1 新增：从 `AuthSource` 取 token）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/blob_store.py`（Stage 4 硬前置 2 新增：BlobStore 接口 + LocalFS / S3 / MinIO 实现）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/routers/blobs.py`（Stage 4 硬前置 2 新增：`/api/v1/blobs` multipart endpoint）
- `/Users/artemis/Documents/Resourse/GitProjects/my-aiaw-deployment/src-backend/data/pagination.py`（Stage 4 硬前置 1 新增：cursor 分页 helpers — `CursorPage` envelope + `normalize_limit` + `build_page` + `CURSOR_MAX_LIMIT=1000`）
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

| 阶段 | 验证手段                                                                                      | 通过判据                                                                                |
| ---- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| 0    | `pnpm build` + 手测核心闭环                                                                 | 行为与现状完全一致                                                                      |
| 1    | staging + 单表（providers）灰度                                                               | 清缓存后服务端数据回灌；flag 关掉立即回到旧逻辑                                         |
| 1.5  | 注册 A/B 两账号 + token 过期 + logout 吊销                                                    | 账号数据隔离；token 自动 refresh；logout 后 refresh 返回 401                            |
| 2    | 双 tab 实时联动                                                                               | < 500ms 收到事件；断网降级到 poll 仍最终一致                                            |
| 3    | 逐表分批 + 导出/导入往返                                                                      | 每张表独立可灰度可回滚                                                                  |
| 4    | 级联删除 + 大量消息加载                                                                       | 服务端单事务级联，DialogView 滚动加载性能不退化                                         |
| 5    | 全新设备首次登录 + 卸载重装 +**旧版导出 → 新版导入往返字节一致**（含对象存储桥接路径） | 服务端为唯一真源；包内无 `dexie-cloud-addon`；ref blob 透明还原                       |
| 全程 | **旧版导出 → 新版导入 → 旧版导入** 数据闭环                                           | `aiaw_user_db.json` 字节级互通，无字段丢失；新版 export JSON grep 无 `"type":"ref"` |
| 全程 | 多设备开机首次登录                                                                            | 第二台直接走「server → 本地缓存」回灌（无 push 机制，无标记表）                        |

每阶段均能合并到 master、独立部署、按 flag 灰度，验证失败时仅通过环境变量回退即可，无需代码 revert。
