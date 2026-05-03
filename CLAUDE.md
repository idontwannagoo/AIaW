# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

AI as Workspace (AIaW) — 跨平台 LLM 客户端，基于 **Quasar 2 + Vue 3 + TypeScript**。同一份前端代码以 SPA / PWA / Tauri 桌面 / Capacitor Android 形式发布。当前 fork 是部署版本，附带一个 Python (FastAPI) 后端用于代理与文档解析。

## plan 文件与长期工作流

本仓库使用 `plans/` 目录维护多份长期工作 plan。**开始任何会话前，必须先 Read `plans/` 下与本次工作主题相关的所有 plan 文件**，从中获取动态进度（当前 Step / commit hash / 已知问题 / 修订记录）。本 CLAUDE.md 仅含静态规则，不含动态进度。

当前在册 plan：

- `plans/cloud-sync-migration.md` —— 云同步从 Dexie Cloud 迁到自家 FastAPI 的多阶段迁移。涉及云同步 / 数据层 / 鉴权 / 实时通道的工作必读。
- `plans/test-infrastructure.md` —— 自动化测试脚手架方案。涉及测试 / 验收手段 / CI / 写新 spec 或加 helper 的工作必读。

新增 plan 时同步在本段追加一行索引。

**plan 文件维护规则（静态，不随进度变化）：**

- **plan 是单一真源**：阶段当前进度、最近完成的 commit、下一步、决策修订都写进 plan 文件，不写进本 CLAUDE.md。新会话从 plan 拿动态状态。
- **每完成一个 Step 必须更新 plan**：在 plan 对应 Step 标 ✅，同步更新「进度快照」段；同一次提交里把代码改动与 plan 更新一起 commit，避免 plan 与代码漂移。**不要在 plan 里写当前 commit 自身的 hash**——commit hash 由内容（含 plan）决定，自指数学上无解（amend 后 hash 漂移，plan 写的 hash 失效）。要追溯具体提交直接 `git log` / `git blame` plan 文件。已完成的前序 step 想顺手附个短 hash 做导航锚 OK，但非必须；标 ✅ + Step 名足以定位。
- **方案有调整必须加修订记录**：当某阶段假设被否定 / 子步骤拆分 / 顺序调整 / 上线策略变化时，在 plan 顶部「修订记录」追加一条带日期 + 背景 + 变更 + 影响范围的条目，再改正文。不要静默改正文。
- **每次代码修改必须配套通过判据**：每个 Step 在 plan 里都有「通过判据」段，代码改动落地后必须按判据真测一遍（curl / Console / 多 tab / 清缓存等），把结果记进进度快照。判据失败时优先修代码，而不是改判据。
- **flag 机制保留作"应急 disable 开关"，不再用作"上线节奏 / 灰度兜底"**（2026-05-02 plan 方向调整修订生效）：new-deploy 是空库新实例 + my-deploy 是天然回滚通道，不需要 new-deploy 上"flag 默认关 + 字节级回滚"的兜底。**Stage 3+ 每张表落地时同一批 commit 内直接把表名加进 `.env.docker` 的 `BACKEND_DATA_TABLES` CSV 并 push 翻开**，不走"上线但不启用 → 后续批次再翻 flag"两步节奏。`BACKEND_DATA_API_URL` / `BACKEND_DATA_TABLES` / `BACKEND_AUTH` / `REALTIME_TRANSPORT` / `BACKEND_DATA_API_ENABLED` 这套 flag 仍然存在（用作 server.ts 写炸时秒级 disable 单表的应急开关），但默认状态从"全关"改成"按 plan 灰度顺序逐档全开"，整套机制最终在 **Stage 4.9 一次性下架**（前提：Stage 4.5 端到端验收通过 + 稳定运行 ≥ 1 周）。云同步重构相关代码**永不合 my-deploy**——详见下方「部署分支拓扑」段。
- **后端模块条件挂载**：新增 backend 子模块（如 Stage 2 `realtime.py`）若依赖 `JWT_SECRET` 等强制 env，必须放在 `_enable_backend_data_api()` flag 守卫的 lazy import 里，不能让无 flag 的 Northflank 部署在 import 期就崩。
- **提交信息**：遵循全局规则（中文、不带 `Co-Authored-By` 与 AI 署名）；项目惯用 `<scope>: <短描述>` 或 `云同步重构stageX-stepY: <内容>` 形式（参考 `git log`）。

## 部署分支拓扑（2026-05-02 起生效）

本仓库有**两个独立部署分支**对应**两个独立 Northflank 服务**，承载两批用户。这是 plan `cloud-sync-migration.md` 2026-05-02 修订记录确定的「导入导出 only」迁移路径的部署侧实现 —— 老用户继续用老实例直到主动迁移，新版独立实例承载全量 stage 1+ backend 功能。

### 分支角色

- **`my-deploy`** —— 老用户兜底实例。HEAD 锁在 Stage 0 抽象层重构 + Dexie 路径 bug fix 干净 baseline（commit `2b26349`）。线上服务连官方 Dexie Cloud SaaS（`znm3rqzc8.dexie.cloud`），数据走 IndexedDB + dexie-cloud-addon 推 SaaS。**不接受任何云同步重构 commit**，包括 Stage 1 / 1.5 / 2 / 3+ 任何后续阶段；只接：上游 `master` 同步、老 Dexie 路径 bug 修复、与云同步无关的 UI / 性能改进。
- **`new-deploy`** —— **新版部署分支（默认工作目标）**。HEAD 在 Stage 2 收官（commit `5c689ab` 起）。包含 Stage 0/1/1.5/2 全套云同步重构 + 测试脚手架（`tests/`） + 全部 plan（`plans/`）+ 本 CLAUDE.md。Stage 3+ 工作只合到这里 → 部署到独立 Northflank 服务 + 独立 Postgres + Stage 4 起独立对象存储 + 独立域名。
- **`backup/my-deploy-pre-reset`** —— safety net 永久分支，指向 reset 前 my-deploy HEAD（`5c689ab`）。半年内不删，作为「万一新拓扑要整体回滚」的逃生通道。

### 工作流契约（变更落到哪个分支的硬性约束）

| 改动类型 | 工作分支 base | merge 目标 |
|---|---|---|
| 上游 `master` 同步 | `master` | `my-deploy` + `new-deploy` 各 merge 一次 |
| 老 Dexie 路径 bug 修复 | `my-deploy` | `my-deploy` → 然后 cherry-pick 或 merge 到 `new-deploy` |
| 云同步 Stage 3+ 新功能 / 新表 / 新 endpoint / spec / plan 修订 | `new-deploy` | **只合 `new-deploy`，永不合 `my-deploy`** |
| 测试脚手架（`tests/`、`docker-compose.test.yml`、`tests/scripts/`） | `new-deploy` | **只合 `new-deploy`** |

违背契约的后果：my-deploy 被 dead weight 污染（Stage 1+ 代码 flag 关时无害但镜像膨胀几十 KB），要清理就得再跑一次 force-push reset，引入额外部署事件。

### Northflank 部署侧约束

- **服务 1（指向 `my-deploy`）**：保留现状。env 不再开 `BACKEND_DATA_API_ENABLED` / `JWT_SECRET` / `BACKEND_AUTH` / `BACKEND_DATA_API_URL` / `BACKEND_DATA_TABLES` / `REALTIME_TRANSPORT` 等任何 backend flag。Stage 5 落地后由 active 用户迁移率（建议 ≥ 80%）触发 6 周下线窗，到期关闭。
- **服务 2（指向 `new-deploy`）**：已上线（公网域名 `https://p01--new-aiaw--hqdb2bsvdnbt.code.run`），独立 Postgres 实例 + Stage 4 起独立 R2/MinIO bucket；env 已开 `BACKEND_DATA_API_ENABLED=true` + `JWT_SECRET=<random>` 启用 backend data API。前端 flag（`BACKEND_AUTH` / `BACKEND_DATA_TABLES` / `REALTIME_TRANSPORT`）按 plan 灰度 —— 注意前端是构建期内联，开 flag 必须改 `new-deploy` 上的 `.env.docker` 后 push 重 build，控制台 env override 对前端无效。当前实例属性：仅适合 dev preview，不要导入真实数据 / 不要邀请他人，直到 Stage 4.5 落地+真实端到端验收通过（详见 plan「上线节奏与人工端到端测试分工」段）。
  - **注册模式（`ALLOW_REGISTRATION` / `INVITE_CODE`）**：`src-backend/data/routers/auth.py:38` 默认值是 `invite`（不是 plan 历史草稿写过的 `true`）。Northflank 控制台当前实际配置 = invite 模式 + `INVITE_CODE=aiaw-2026-beta`。前端 / smoke 脚本 / 任何调 `POST /api/v1/auth/register` 的客户端必须在 body 里带 `invite_code` 字段，否则 403。三种值的语义：`true` 全开 / `invite`（默认）需 INVITE_CODE 匹配 / `false` 完全关。**轮换 invite code**：改 Northflank 控制台 `INVITE_CODE` env → 服务自动重启（runtime env，不需重 build）→ 同步改本段记录值；不要把 INVITE_CODE 写进 `.env.docker`（那是构建期内联到前端 JS bundle 的，会泄露给所有访客）。Stage 4.5 之前不公开传播 invite code，仅自己测 + 内圈使用。

### 老用户迁移路径（导入导出 only）

老用户在 `my-deploy` 域名上 ExportDataDialog 导出 `aiaw_user_db.json` → 在 `new-deploy` 域名上 ImportDataDialog 导入。Stage 4.5 落地后导入走 server-side worker（浏览器只切片直传）。new-deploy 镜像不挂 `dexie-cloud-addon`（Stage 2.5 已卸），不愿迁的老用户继续用 my-deploy 不动；存在 bug 时回退路径就是「用回 my-deploy URL」，零数据风险。

### 部署分支 reset 操作 SOP（如未来再需要）

force-push deploy 分支是分水岭操作，必须按 4 步走：① 在远端创建 `backup/<branch>-pre-reset` safety net 锚定当前 HEAD；② 创建承接代码的新分支并 push；③ 本地 `git reset --hard <target>`；④ `git push --force-with-lease origin <branch>`（不要用 `--force`）。每步独立验证后再继续。

## 测试体系（静态规则）

测试体系的动态进度与 Phase 拆分见 `plans/test-infrastructure.md`；**操作手册（怎么跑、加 spec 配方、helper 对照表、调试排查、守则）见 `tests/README.md`**——做任何测试相关操作前必须先 Read。本段仅列不随进度变化的稳定规则：

### 架构与环境

- **两层架构**：后端走 pytest（`tests/api/`），端到端走 Playwright（`tests/e2e/`）。不引入 Vitest 单元层（投资回报低，本项目 bug 几乎全是多端协作 / 时序 / 网络型）。
- **环境完全隔离**：测试用 Postgres 5434 / backend 9011 / 前端 9007/9008/9009 三 profile 端口，与 dev 的 5433/9010/9005 互不影响。任何测试脚本不得读写 dev 用的 Postgres / 9010 backend；临时改 `.env.local` 必须带还原 trap，无论成功失败或中断都还原。
- **e2e 跑 build 产物，不跑 dev server**：每个 flag profile 一份独立 `quasar build` 产物，按 (env-sha256, git rev, package.json hash) 联合 cache 复用。原因是 `process.env.*` 构建期内联，runtime 切 flag 不可行。
- **profile 选择**：`baseline`（全 flag 关）/ `providers-rest`（backend providers + BACKEND_AUTH，无 realtime）/ `realtime-ws`（上面 + REALTIME_TRANSPORT=ws）。新加表或 flag 时按 cloud-sync-migration plan 选合适 profile，不够用就新增 profile（同步加 `playwright.config.ts` project + 端口 + `tests/env/.env.test.<name>`）。
- **调试钩子守卫**：给 e2e 暴露的 `window.__db__` / `window.__authSource__` / `window.__repos__` / `window.aiawRealtime` 等钩子必须放在 `EXPOSE_DB=true` env 守卫的 boot 文件里；prod docker build 流程必须断言此 env 非 true 才允许出包，避免暴露 IndexedDB 操作面。
- **测试命令命名**：测试相关 pnpm script 统一 `test:` 前缀；退出码 0 = 全绿、非 0 = 失败数；reporter 输出统一进 `tests/.results/`。
- **dexie-cloud SaaS 不进 e2e 默认路径**：测试 env 默认关 `DEXIE_DB_URL`，只测 backend 路径；要测 dexie auth 链路时单独开 profile，必要时用 helper 拦 host 模拟 SaaS down。避免 e2e 依赖外部 SaaS 可用性。

### 工作流（每个需求都必须遵守）

- **判据即代码**：`plans/cloud-sync-migration.md` 每条「通过判据」必须映射到具体 pytest test name 或 Playwright spec name，并在 plan 对应 Step 段尾写下 `- api: tests/api/<file>::<test>` / `- spec: tests/e2e/<path>::<test>` 引用。手测判据不再视为完成标准。
- **新需求落地 = 同批次落 spec + 真跑过测试**：任何新 Step / 新功能 / 新表 / 新 endpoint / bug 修复，**代码变更必须在同一批次里附对应自动化 case**，并在本地真跑过 `pnpm test:api && pnpm test:e2e`。三种情况都视为未完成、不允许 commit / push：① 只改代码不写 spec；② 写了 spec 但本地没跑；③ 跑了但有非「文档化预期红」的 case 红。回归套件持续累积，不允许欠账。
- **TDD 默认顺序**：spec-first → 跑红（确认 spec 真在卡功能）→ 写代码 → 跑绿。Phase 5 的 step4 spec 就是范例（红的输出本身证明 Step 4 未做）。无法先写 spec 的纯重构 / 配置改动可以后置 spec，但同批次必须有。
- **改动什么 → 跑什么**：
  - 改 `src-backend/data/`（路由 / 模型 / auth / realtime / migration）→ 必跑 `pnpm test:api`，必要时跑 `pnpm test:e2e -g <相关 spec>`
  - 改 `src/data/`（repos / auth source / sync source / http / realtime-ws）→ 必跑 `pnpm test:e2e`（至少跑相关 stage 的 spec）
  - 改 `src/boot/expose-debug.ts` 或 `quasar.config.js` 的 boot 列表 → 必跑 `pnpm test:e2e -g smoke`
  - 改 `tests/env/.env.test.<profile>` → cache 自动 miss 重 build，必跑全套 `pnpm test:e2e`
  - 改 `tests/scripts/` / `playwright.config.ts` / `pytest.ini` / `docker-compose.test.yml` → 必跑全套 `pnpm test:api && pnpm test:e2e`
  - 改 `src/i18n/`（影响 e2e selector 的标签）→ 跑相关 spec 验证 selector 仍命中
  - 改 `src/utils/db.ts`（Dexie schema）→ 跑相关 stage spec + 手测一次升级路径（schema 升级目前没自动 case，是 Stage 3+ 待补）
  - 纯前端 UI 视图（`src/pages/` / `src/components/` / `src/views/`）改样式 / 文案 → 不强制跑 e2e，但若改 selector-relevant 文本需评估
- **commit 前必跑**：`pnpm test:api` + 与改动相关的 `pnpm test:e2e -g <pattern>`。整套 e2e 跑全 profile ~1min，commit 前推荐跑全套；至少跑相关项目（`--project=<profile>`）。
- **判据失败优先修代码，不优先改 spec 预期值**：spec 红了先把它当真信号，沿着 stdout 给的 `lastA / lastB / elapsedMs / status / body` 等具体值定位代码 bug；只有确认 spec 假设本身错了（plan 改了 / 接口换了）才动 spec。
- **新 spec 落地后必须真跑红一次再跑绿**：写完 spec 不要立刻跑绿就 commit，先想办法构造一次预期失败（注掉一行核心代码 / 改个返回码），确认 spec stdout 输出包含足够定位信息（具体 status + body + dict + elapsed），再恢复并跑绿。这是 plan Phase 3/4/5 验收都要求的「故障注入红测」习惯。
- **测试命令最短形式**（详细见 `tests/README.md`）：
  ```bash
  pnpm test:api                       # 全 pytest（~50s）
  pnpm test:api -k <substring>        # 单 case 调试
  pnpm test:api -m "not slow"         # 跳 slow mark
  pnpm test:e2e                       # 全 Playwright（~1min）
  pnpm test:e2e -g <pattern>          # 单/多 spec
  pnpm test:e2e --project=<profile>   # 单 profile
  pnpm test:up / test:down            # docker 5434 起停
  pnpm test:backend:start/stop        # 9011 backend 起停
  ```
- **新建 spec 必读 helper 对照表**：写 spec 前必须 Read `tests/README.md` §5 的 helper ↔ plan 词汇表，**优先用现有 helper 而不是手写**（`expectRowSync` / `setOffline` / `injectAuth` / `openContextsForUsers` / `dumpTable` / `pgQuery` / `captureWs` / `backendClient` 等已经覆盖大部分形状）。需要新 helper 时加进 `tests/e2e/helpers/`，同步在 README §5 表里加一行。
- **README 维护**：tests/README.md 的「判据映射表」「已知预期红」「helper ↔ plan 词汇」「调试排查」「守则」段在以下情况必须同步更新：① 新增 / 删除 helper；② 已知预期红转绿或新增；③ 加新 profile / 端口；④ 加新顶层 `pnpm test:` script；⑤ 工作流守则发生变化。

## 常用命令

包管理器使用 **pnpm**（仓库已带 `pnpm-lock.yaml`）。

```bash
pnpm i                       # 安装依赖
pnpm dev                     # = quasar dev，默认 SPA 模式，端口 9005
quasar dev -m pwa            # 以 PWA 模式启动，端口 9006
quasar dev -m capacitor -T android   # Android (Capacitor)
pnpm build                   # = quasar build (SPA)
quasar build -m pwa          # 构建 PWA（Dockerfile 用此命令）
pnpm lint                    # ESLint 全量
pnpm sync-version            # 把 src/version.json 同步到 tauri.conf.json / package.json / android build.gradle
pnpm docs:dev                # 启动文档站点 (vitepress)
```

`pnpm test` 当前是 placeholder（`echo "No test specified"`），仓库无测试套件。

类型检查与 lint 在 `quasar dev/build` 期间通过 `vite-plugin-checker` 自动跑（`tsconfig.vue-tsc.json` + ESLint）。单独跑类型检查没有内置脚本，可用 `pnpm exec vue-tsc -p tsconfig.vue-tsc.json --noEmit`。

### Docker 构建

`Dockerfile` 是两阶段：第一阶段 `pnpm build -m pwa` 生成 `dist/pwa`；第二阶段把产物挂到 `src-backend/app.py`（FastAPI）下作为静态文件，端口 9010。`.env.docker` 是构建时的前端环境变量（被复制为 `.env.local`）；`.env.app` 是官方部署用的同款变量集合，可用于本地起 dev。

## 架构关键点

### 配置入口与多形态打包
- `quasar.config.js` 是唯一的应用配置入口：boot files (`src/boot/`) → `i18n` / `unocss` / 全局组件；启用的 Quasar 插件 = `Notify / Dark / Dialog / Loading`；Vite 自带 `@intlify/unplugin-vue-i18n`、`vite-plugin-checker`、`unocss/vite`。
- 同一份 `src/` 编译成多种形态：`src-pwa/`（自定义 service worker）、`src-tauri/`（Rust 桌面壳）、`android/` + `capacitor.config.ts`（移动端）、`src-backend/`（仅 Docker 部署里用到的 Python 代理）。涉及平台差异的代码优先走 `src/utils/platform-api.ts`（`IsTauri`、`fetch`、`tauri-stream`、`tauri-shell-transport`）而不是直接用浏览器 API。

### 数据层（核心）
- **Dexie + Dexie Cloud**：`src/utils/db.ts` 定义所有 IndexedDB 表（`workspaces / dialogs / messages / assistants / artifacts / installedPluginsV2 / reactives / avatarImages / items / providers`），当前 schema 版本是 `db.version(6)`。**改动 schema 必须递增版本号并写迁移**（仓库里所有迁移都是用 `db.<table>.hook('reading', ...)` 形式做向后兼容读取，例如 v1.4、v1.8 的迁移示例）。
- 启用 Dexie Cloud 取决于环境变量 `DEXIE_DB_URL`（在 `src/utils/config.ts` 暴露为 `DexieDBURL`）；为空时自动跳过云端 addon 与登录路由（见 `router/routes.ts` 里的条件 `/account` 与 `/model-pricing` 路由）。
- 配置全部从 `process.env.*` 进 `src/utils/config.ts` 读：`DOC_PARSE_BASE_URL / CORS_FETCH_BASE_URL / SEARXNG_BASE_URL / DEXIE_DB_URL / LITELLM_BASE_URL / BUDGET_BASE_URL / SYNC_SERVICE_PRICE* / DISABLE_CHECK_UPDATE / MAX_MESSAGE_FILE_SIZE_MB`。改了变量名要同时改 `.env.app` / `.env.docker`。

### 状态管理
- **Pinia** stores 在 `src/stores/`：`workspaces / assistants / providers / plugins / user-data / user-perfs / ui-state`。多数 store 用 `dexie-cloud-addon` 的 `liveQuery` + `useObservable`（见 `composables/live-query.ts`）把 Dexie 表实时映射成响应式数据 — **不要在组件里直接 `db.<table>.toArray()`**，走 store / live-query 才能拿到跨标签页同步与云端同步。
- 持久化的本地配置走 `composables/persistent-reactive.ts` / `local-reactive.ts`（封装在 `reactives` 表上）。

### 路由与视图
- `src/router/routes.ts` 是单一路由表，全部嵌套在 `MainLayout` 下。语义层级是 `/workspaces/:wsId/{dialogs,assistants,settings}/...`、`/plugins/...`、`/assistants/...`、`/settings/...`。`pages/` 是路由页骨架（带导航栏/侧栏），`views/` 是被嵌入到 page outlet 的内容视图 — 添加新功能页时按这个分层放。
- 国际化：`src/i18n/`，由 `boot/i18n.ts` 注册；写组件文案统一走 `t('...')`，不要硬编码。

### LLM / 插件 / MCP
- 模型供应商：通过 Vercel `ai` SDK + 各家 `@ai-sdk/*` provider；自定义供应商存在 `providers` 表里，UI 在 `views/CustomProvider.vue`。调用入口在 `composables/call-api.ts` + `utils/middlewares.ts` + `utils/sessions.ts`。
- 插件系统：`src/utils/plugins.ts` 定义 builtin / Gradio / LobeChat / MCP 几种插件来源；MCP 客户端在 `utils/mcp-client.ts`，传输层有 `mcp-sse-transport.ts`（Web）、`tauri-shell-transport.ts`（桌面 stdio）。新增插件类型时同时改 `types.ts` 里的判别联合 + `installedPluginsV2` 表的迁移。
- Artifacts：`utils/artifacts-plugin.ts` + `composables/create-artifact.ts` / `close-artifact.ts`。

### 后端（仅部署形态）
- `src-backend/app.py` 是 FastAPI，提供 `/cors/proxy`（白名单代理，见 `ALLOWED_PREFIXES`）和 `/doc-parse/parse`（LlamaParse 文档解析）。前端通过 `CORS_FETCH_BASE_URL` / `DOC_PARSE_BASE_URL` 指过去。前端能独立跑（直连官方 `https://aiaw.app/...` 即可）；改后端必须同步前端 base URL。

## 代码规范要点

- ESLint 配置 = `standard` + `plugin:vue/vue3-strongly-recommended` + `@typescript-eslint`；引号统一单引号，禁用 `no-redeclare`（用 TS 版本），允许 `any`。
- 路径别名走 Quasar 默认：`src/` 在多数 import 里写成 `src/...`，`pages/`、`layouts/`、`components/` 在路由里有顶层别名。新文件保持已有风格。
- UnoCSS 用于原子 class（`uno.config.ts`）；样式优先用 UnoCSS / Quasar variables，避免新增自定义 SCSS。

## 容易踩的坑

- 修改 `db.ts` 的表结构后要：①递增 `db.version(N)` ②补 `hook('reading', ...)` 兼容旧记录 ③更新 `src/utils/types.ts` 对应 interface。漏一步会让旧客户端打不开数据库。
- 涉及登录 / 同步 / 账户余额的 UI 在没有 `DEXIE_DB_URL` / `LITELLM_BASE_URL` 时会被路由层条件性禁用 — 改这部分要测两种环境。
- `src-tauri/` 与 `android/` 目录的版本号由 `pnpm sync-version` 统一从 `src/version.json` 推下去；不要单独手改其中之一。
- Dev server 默认监听 9005（SPA）或 9006（PWA），与后端 9010 不冲突；同时跑 dev + docker 时注意端口。
- **`process.env.FOO === 'true'` 永远是 false**：Quasar (`@quasar/app-vite/lib/utils/env.js`) 把 .env 里值为 `true`/`false` 的变量直接内联成 JS 布尔字面量而不是字符串。读布尔型环境变量必须写 `String(process.env.FOO) === 'true'`，否则在 .env 形式下永远拿不到 true。注意只对布尔字面量这么处理，其他值仍是 JSON.stringify 后的字符串。
- 改 `.env.local` / `.env.app` / `.env.docker` 等 .env 文件后必须**完整重启 dev server**（`process.env.*` 是构建期内联，HMR 不会重新求值）；只改代码可以走 HMR。
- **测试 build cache 不感知工作树 diff**：`tests/scripts/build-frontend-profile.mjs` 的 cache key = `(env-sha256, git rev-parse HEAD, package.json hash)`，不含未提交的 `src/` 改动。改了源码但没 commit 就跑 `pnpm test:e2e`，cache 命中旧产物 → spec 仍跑老代码（红的现象与代码没生效的现象一致，极易误判）。两种解法二选一：① 先 commit 让 git rev 推进；② `rm -rf tests/.builds/<profile>` 强制 rebuild。e2e 红时排除假阴性的第一步是确认 build 是新的。
- **server-routed 表的 WS event `row` 是完整 envelope，不是 unwrap 过的业务对象**：`src-backend/data/routers/<table>.py::_to_event` 与 `_to_row` 共用同一份 envelope schema（如 `providers` 是 `{id, version, updated_at, deleted, data: <CustomProvider>}`），WS 推过来的 `row` 字段也是这个 envelope。`<table>.server.ts` 的 realtime handler 必须像 `pull()` 那样 `db.<table>.put(e.row.data)` 解包，不能直接 `db.<table>.put(e.row)`，否则 cache 形状与 REST 写路径不一致，跨 tab 同步看着「到了」但 schema 错位（典型症状：expectRowSync 输出 A=unwrap / B=envelope）。Stage 3+ 每张新搬到后端的表都要遵守这个契约。
