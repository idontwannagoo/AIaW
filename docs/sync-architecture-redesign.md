# 云同步架构重构方案

## 1. 背景与问题

当前项目基于 **Dexie Cloud** 实现多端同步，核心问题：

1. **绑定 SaaS**：Dexie Cloud 依赖官方订阅，无法自部署。
2. **本地优先架构的代价**：新设备登录需全量拉取整个数据库才能开始使用；低端设备在数据量较大时可能无法完成拉取。
3. **二进制同步过重**：图片 / 文件以 `ArrayBuffer` 存在 IndexedDB 中（[`avatarImages.contentBuffer`](../src/utils/types.ts)、[`items.contentBuffer`](../src/utils/types.ts)），Dexie Cloud 协议通过 WebSocket 序列化为 base64 传输，体积膨胀约 33%，且与对话元数据共用同一同步通道。
4. **数据库不可分区**：所有表全量同步，无法按 workspace / dialog 粒度懒加载。

## 2. 设计目标

| 目标 | 说明 |
|---|---|
| 脱离 Dexie Cloud SaaS | 可自部署，完全掌控数据 |
| 服务端中心（server-authoritative） | 服务端为唯一真相来源，客户端按需拉取 |
| 解决大库卡死 | 打开 workspace 才拉 dialogs，打开 dialog 才拉 messages |
| 二进制走对象存储 | 图片、文件不再走同步协议，改为 S3/MinIO 预签名 URL |
| 导入导出 JSON 格式保持兼容 | 新旧版本导出文件互通，零改动用户备份 |
| 渐进式上线 | 可分阶段交付，早期保持本地库形态不变 |

## 3. 目标架构总览

```
┌───────────────────────────────────────────────────────────────┐
│  浏览器 / 客户端                                               │
│                                                                │
│  Vue / Pinia Stores / useLiveQuery                            │
│           │                                                    │
│           ▼                                                    │
│  ┌──────────────┐     ┌───────────────────────┐               │
│  │  Dexie (本地  │◄───►│  sync-client.ts       │──┐            │
│  │   缓存层)    │     │  (写穿透 / 拉取 / 订阅)│  │            │
│  └──────────────┘     └───────────────────────┘  │            │
│           │                                        │            │
│           ▼ dexie-export-import                   │            │
│   导入/导出 JSON (格式与旧版完全一致)              │            │
└─────────────────────────────────────────────────── │ ───────────┘
                                                     │ HTTPS / WSS
┌─────────────────────────────────────────────────── ▼ ───────────┐
│  后端 (扩展 src-backend/ FastAPI)                                │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌────────────┐  │
│  │  Auth    │  │  REST    │  │  WebSocket   │  │  S3 签名   │  │
│  │  (JWT)   │  │  CRUD    │  │  变更推送    │  │  (可选)    │  │
│  └──────────┘  └──────────┘  └──────────────┘  └────────────┘  │
│           │          │                │                │         │
│           └──────────┴────────────────┘                │         │
│                      ▼                                 ▼         │
│              ┌──────────────┐               ┌──────────────┐    │
│              │  PostgreSQL  │               │  MinIO / S3  │    │
│              │  (元数据)    │               │  (二进制)    │    │
│              └──────────────┘               └──────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

核心原则：
- **服务端是真相来源**。所有写操作走 API，成功后回写本地 Dexie。
- **本地 Dexie 是缓存 + 离线队列**。schema 和字段完全保留，保证导入导出 JSON 兼容。
- **按需加载**。pinia store 从"订阅全表"改为"订阅当前作用域的 slice"。

## 4. 数据分类与存储策略

| 类别 | 表 | 本地 | 服务端 | 二进制位置 |
|---|---|---|---|---|
| 元数据（小） | `workspaces`、`assistants`、`providers`、`installedPluginsV2`、`reactives` | 全量缓存 | Postgres | — |
| 元数据（中） | `dialogs`、`artifacts` | 按 workspace 懒加载 | Postgres | — |
| 元数据（大） | `messages`、`items` | 按 dialog 懒加载 | Postgres | — |
| 二进制 | `avatarImages.contentBuffer`、`items.contentBuffer` | 按需下载 + LRU 缓存 | Postgres 只存 URL / key | MinIO / S3 |

## 5. 同步模型

### 5.1 读路径（按需拉取）

```
组件订阅 store.dialogs(workspaceId)
  └─ store 先返回本地 Dexie 中的数据（立即渲染）
  └─ 并发请求 GET /api/workspaces/{id}/dialogs?since=<localMaxUpdatedAt>
       └─ 拿到增量 → 写入本地 Dexie → live query 自动触发 UI 更新
```

关键点：
- 每个实体记录 `updated_at`（服务端权威时间戳），客户端用 `since` 做增量拉取。
- 二进制不在 `GET` 响应中返回，只返回 URL / key；组件用到时再单独 fetch，下载后可选缓存进 `avatarImages` / `items` 的 `contentBuffer`。

### 5.2 写路径（写穿透 + 离线队列）

```
用户操作 → store.updateXxx(id, changes)
  └─ 1. 立即写本地 Dexie（UI 无延迟）
  └─ 2. 推到发送队列 outbox 表
  └─ 3. sync-client 异步 POST/PUT 服务端
       ├─ 成功：从 outbox 删除，用服务端返回的 updated_at 回填
       └─ 失败：保留在 outbox，网络恢复后重试
```

离线队列表（新增）：

```
outbox: ++id, createdAt, entity, op, payload, retryCount
```

### 5.3 变更推送（实时同步）

- **最小可行**：客户端空闲时每 N 秒对每个"打开中的作用域"跑一次 `since` 增量拉取（轮询）。
- **推荐**：WebSocket `/ws` 通道，服务端在写入 Postgres 后广播 `{entity, id, updatedAt}` 通知；客户端收到后针对性拉取该条记录。
- 不走全量 CRDT / OT，因为应用是单用户多端场景，**后写胜出（last-write-wins）** 按 `updated_at` 判断即可。

### 5.4 冲突处理

- 同一字段的并发修改：服务端 `updated_at` 晚者覆盖，客户端本地版本被服务端版本替换。
- 删除 vs 修改：服务端使用 tombstone（`deleted_at`）软删，客户端收到后清理本地。
- 文件上传失败：`items` 记录进入本地 "draft" 状态，不同步到服务端，直到上传成功。

## 6. 二进制（图片 / 文件）存储

### 6.1 上传流程

```
1. 客户端算出文件 sha256 作为 key
2. POST /api/files/sign  body: { key, size, mimeType }
   → 返回预签名 PUT URL
3. PUT 到 MinIO / S3
4. POST /api/items  body: { id, fileKey, mimeType, ... }
5. 服务端持久化元数据
```

### 6.2 本地 DB 结构保留

关键：**不删 `contentBuffer` 字段**，保证导入导出 JSON 结构不变。

新版 `StoredItem` 字段并存：

```ts
interface StoredItem {
  id: string
  dialogId: string
  type: 'text' | 'file' | 'quote'
  contentText?: string
  contentBuffer?: ArrayBuffer   // 旧字段，仍可存（离线编辑 / 刚创建未上传时）
  fileKey?: string              // 新增：S3 key
  fileUrl?: string              // 新增：可直接展示的 URL（可选，运行时缓存）
  name?: string
  mimeType?: string
  references: number
}
```

组件读取策略（见 [`src/composables/file-url.ts`](../src/composables/file-url.ts)）：

```
if (contentBuffer) return blobURL(contentBuffer)        // 本地已有 → 零网络
else if (fileUrl)  return fileUrl                        // 走 CDN
else if (fileKey)  return fetch(signedGet(fileKey))      // 按需下载
```

## 7. 导入导出兼容性保证

现状（[`ExportDataDialog.vue`](../src/components/ExportDataDialog.vue) + [`ImportDataDialog.vue`](../src/components/ImportDataDialog.vue)）调用 `dexie-export-import`，它只看**本地 Dexie schema**。

**硬性约束**：
1. 不 bump `db.version`（维持 `version(6)`）或升级时保持**字段向后兼容**。
2. 不重命名任何已有表和字段。
3. 二进制字段 `contentBuffer` 继续在 schema 中存在（即使通常为空）。
4. `owner` / `realmId` 保留为可选字段（默认 `'unauthorized'`），兼容 [`ExportDataDialog.vue` 第 63 行](../src/components/ExportDataDialog.vue#L63) `removeUserMark` 的 transform 逻辑。

### 导出时回填 buffer

若本地已切换为"只存 URL / key"，导出前需现场拉取二进制回填：

```ts
// ExportDataDialog.vue 内部
exportDB(db, {
  ...options,
  transform: async (table, value, key) => {
    if (table === 'items' && value.fileKey && !value.contentBuffer) {
      value.contentBuffer = await downloadAsArrayBuffer(value.fileKey)
    }
    return { value }
  }
})
```

导出 JSON 格式与旧版**逐字节兼容**，旧版客户端导入新版备份无障碍。

### 导入后推送到服务端

```ts
// ImportDataDialog.vue .then(() => { ... }) 里加一行：
await syncClient.pushAll()
```

把本地刚导入的数据批量推到服务端，完成端到端恢复。

## 8. 认证方案

替换 Dexie Cloud 的 Email OTP：

- 后端提供 `/auth/request-otp`、`/auth/verify-otp` → 颁发 JWT
- 前端 [`src/composables/login-dialogs.ts`](../src/composables/login-dialogs.ts) 的 `db.cloud.userInteraction` / `db.cloud.currentUser` 订阅替换为本地 store + fetch 调用
- JWT 存 localStorage，sync-client 所有请求自动附带 `Authorization: Bearer <token>`

## 9. 后端数据模型（Postgres 草案）

```sql
create table users (
  id uuid primary key,
  email text unique not null,
  created_at timestamptz default now()
);

create table workspaces (
  id uuid primary key,
  user_id uuid references users(id),
  parent_id uuid,
  type text,
  data jsonb not null,           -- 其余字段整体存 jsonb，演化友好
  updated_at timestamptz default now(),
  deleted_at timestamptz
);
create index on workspaces(user_id, updated_at);

-- dialogs / assistants / artifacts / providers / messages / items / ...
-- 同构：user_id + 主键 + data jsonb + updated_at + deleted_at
-- messages / items 额外带 dialog_id 索引

create table files (
  key text primary key,           -- sha256
  user_id uuid,
  mime_type text,
  size bigint,
  created_at timestamptz default now()
);
```

用 `jsonb` 存业务字段而非拆列，是因为前端 schema 演化快、字段数多；查询场景主要是按 owner + 索引字段过滤，不需要复杂 SQL。

## 10. 前端改造清单

| 模块 | 文件 | 改动 |
|---|---|---|
| DB 初始化 | [`src/utils/db.ts`](../src/utils/db.ts) | 移除 `dexieCloud` addon 与 `db.cloud.configure`，保留 schema；新增 `outbox` 表 |
| 登录 | [`src/composables/login-dialogs.ts`](../src/composables/login-dialogs.ts) | 替换为后端 OTP + JWT |
| 同步客户端 | `src/utils/sync-client.ts`（新） | REST / WS 封装、写穿透、增量拉取、outbox 重试 |
| 文件抽象 | [`src/composables/file-url.ts`](../src/composables/file-url.ts)、[`src/composables/avatar-image.ts`](../src/composables/avatar-image.ts) | 优先 buffer、再 URL、再远程拉取 |
| Avatar 上传 | [`src/components/PickAvatarDialog.vue`](../src/components/PickAvatarDialog.vue) | 改为走预签名 URL 上传；保留 `contentBuffer` 字段用于离线 |
| Store | [`src/stores/*.ts`](../src/stores/) | 写操作追加 `syncClient.push`；订阅从全表改为按作用域 |
| 导出 | [`src/components/ExportDataDialog.vue`](../src/components/ExportDataDialog.vue) | 新增 `transform` 回填 buffer |
| 导入 | [`src/components/ImportDataDialog.vue`](../src/components/ImportDataDialog.vue) | 成功回调追加 `syncClient.pushAll()` |

## 11. 后端改造清单（`src-backend/`）

| 模块 | 内容 |
|---|---|
| 依赖 | `sqlalchemy` / `asyncpg` / `alembic` / `pyjwt` / `boto3` 或 `minio` |
| 路由 | `/auth/*`、`/api/<entity>` CRUD、`/api/files/sign`、`/ws` |
| 中间件 | JWT 鉴权、rate limit |
| 模型 | 对应 §9 的 ORM 定义 |
| 部署 | 追加 `docker-compose`：postgres + minio + app，沿用现有 `Dockerfile` 模式 |

## 12. 分阶段落地

### 阶段 1：脱离 Dexie Cloud（~2 周）
- 后端 CRUD + Auth + 基础 sync-client
- 本地 Dexie 结构零变更，仍存 ArrayBuffer
- 所有数据走服务端，但**暂时仍全量同步**
- 目标：摆脱 SaaS 依赖；导入导出零改动

### 阶段 2：懒加载 + 对象存储（~3-4 周）
- store 订阅改造为按作用域
- 文件上传改走 S3，`items` / `avatarImages` 新增 URL 字段
- 导出时 transform 回填 buffer
- 目标：解决大库卡死与低端设备问题

### 阶段 3：优化（可选）
- WebSocket 实时推送替代轮询
- 本地 LRU 文件缓存淘汰策略
- 二进制断点续传

## 13. 风险与边界

| 风险 | 缓解 |
|---|---|
| 离线编辑合并冲突 | 采用 last-write-wins + `updated_at`；必要字段（如 messages 内容）使用追加模型 |
| 文件上传失败阻塞消息发送 | 本地消息可标记 `pending`，允许重试；发送队列与业务 UI 解耦 |
| Dexie schema 冻结限制演化 | 未来新增字段追加而非重命名；老字段保留 deprecated 但不删 |
| 服务端故障时用户可用性 | 客户端继续使用本地缓存（只读）；写入进 outbox 待恢复 |

## 14. 要点速览

- **保留本地 Dexie，仅替换同步层**，是导入导出兼容的根本保障。
- **服务端权威**简化了冲突处理，避免引入 CRDT 复杂度。
- **二进制与元数据分离**是解决核心痛点的关键一步，必须做。
- **字段只增不改**是 schema 演化铁律。
