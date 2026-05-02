export const MaxMessageFileSizeMB = parseFloat(process.env.MAX_MESSAGE_FILE_SIZE_MB || '20')
export const DocParseBaseURL = process.env.DOC_PARSE_BASE_URL
export const CorsFetchBaseURL = process.env.CORS_FETCH_BASE_URL
export const SyncServicePrice = process.env.SYNC_SERVICE_PRICE && parseFloat(process.env.SYNC_SERVICE_PRICE)
export const SyncServicePriceUSD = process.env.SYNC_SERVICE_PRICE_USD && parseFloat(process.env.SYNC_SERVICE_PRICE_USD)
export const UsdToCnyRate = process.env.USD_TO_CNY_RATE && parseFloat(process.env.USD_TO_CNY_RATE)
export const StripeFee = process.env.STRIPE_FEE && parseFloat(process.env.STRIPE_FEE)
// Stage 1+ self-hosted backend. Empty = no backend, behave like Stage 0.
export const BackendApiBaseURL = process.env.BACKEND_DATA_API_URL
// Stage 1 Step 6: CSV allowlist of table names that route to the backend
// instead of Dexie. Empty = all tables stay on Dexie (Stage 0 behavior).
// Only honored when BackendApiBaseURL is set. Unknown table names are ignored.
export const BackendDataTables = new Set(
  (process.env.BACKEND_DATA_TABLES ?? '').split(',').map(s => s.trim()).filter(Boolean)
)
// Why String(): Quasar inlines `FOO=true` from .env files as the JS boolean
// literal `true` (see @quasar/app-vite/lib/utils/env.js), so `=== 'true'`
// would always be false. Coerce so both inlined boolean and string forms work.
export const BackendAuth = String(process.env.BACKEND_AUTH) === 'true'
// Stage 2: 实时通道传输形态。Step 4 仅识别 'ws'；'sse' / 'poll' / 'auto' 留给 Step 5。
// 空字符串 = 关闭实时通道，写入靠下一次 list() / 刷新拉取（Stage 1 行为）。
export const RealtimeTransport = (process.env.REALTIME_TRANSPORT ?? '').trim()
export const LitellmBaseURL = process.env.LITELLM_BASE_URL
export const BudgetBaseURL = process.env.BUDGET_BASE_URL
export const SearxngBaseURL = process.env.SEARXNG_BASE_URL
export const DisableCheckUpdate = String(process.env.DISABLE_CHECK_UPDATE) === 'true'
