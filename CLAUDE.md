# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

AI as Workspace (AIaW) — 跨平台 LLM 客户端，基于 **Quasar 2 + Vue 3 + TypeScript**。同一份前端代码以 SPA / PWA / Tauri 桌面 / Capacitor Android 形式发布。当前 fork 是部署版本，附带一个 Python (FastAPI) 后端用于代理与文档解析。

## 长期迁移工作流（云同步重构）

本仓库正在执行云同步从 Dexie Cloud 迁到自家 FastAPI 的多阶段迁移，**权威计划在 `plans/cloud-sync-migration.md`**。该文件含完整阶段拆分、Step 通过判据、上线/回滚策略、修订记录、进度快照。涉及云同步 / 数据层 / 鉴权 / 实时通道的任何工作前必须先 Read。

**plan 文件维护规则（静态，不随进度变化）：**

- **plan 是单一真源**：阶段当前进度、最近完成的 commit、下一步、决策修订都写进 plan 文件，不写进本 CLAUDE.md。新会话从 plan 拿动态状态。
- **每完成一个 Step 必须更新 plan**：在 plan 对应 Step 标 ✅ + commit 短 hash；同步更新「进度快照」段；同一次提交里把代码改动与 plan 更新一起 commit，避免 plan 与代码漂移。
- **方案有调整必须加修订记录**：当某阶段假设被否定 / 子步骤拆分 / 顺序调整 / 上线策略变化时，在 plan 顶部「修订记录」追加一条带日期 + 背景 + 变更 + 影响范围的条目，再改正文。不要静默改正文。
- **每次代码修改必须配套通过判据**：每个 Step 在 plan 里都有「通过判据」段，代码改动落地后必须按判据真测一遍（curl / Console / 多 tab / 清缓存等），把结果记进进度快照。判据失败时优先修代码，而不是改判据。
- **flag 默认关 = 字节级一致**：任何阶段的代码合并到 my-deploy 时，前端 `BACKEND_DATA_API_URL` / `BACKEND_DATA_TABLES` / `BACKEND_AUTH` / `REALTIME_TRANSPORT` 与后端 `BACKEND_DATA_API_ENABLED` 默认全不开，行为必须与上一阶段完全一致。这是回滚兜底，不可破坏。
- **后端模块条件挂载**：新增 backend 子模块（如 Stage 2 `realtime.py`）若依赖 `JWT_SECRET` 等强制 env，必须放在 `_enable_backend_data_api()` flag 守卫的 lazy import 里，不能让无 flag 的 Northflank 部署在 import 期就崩。
- **提交信息**：遵循全局规则（中文、不带 `Co-Authored-By` 与 AI 署名）；项目惯用 `<scope>: <短描述>` 或 `云同步重构stageX-stepY: <内容>` 形式（参考 `git log`）。

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
