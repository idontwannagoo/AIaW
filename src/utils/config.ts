export const MaxMessageFileSizeMB = parseFloat(process.env.MAX_MESSAGE_FILE_SIZE_MB || '20')
export const DocParseBaseURL = process.env.DOC_PARSE_BASE_URL
export const CorsFetchBaseURL = process.env.CORS_FETCH_BASE_URL
export const SyncServicePrice = process.env.SYNC_SERVICE_PRICE && parseFloat(process.env.SYNC_SERVICE_PRICE)
export const SyncServicePriceUSD = process.env.SYNC_SERVICE_PRICE_USD && parseFloat(process.env.SYNC_SERVICE_PRICE_USD)
export const UsdToCnyRate = process.env.USD_TO_CNY_RATE && parseFloat(process.env.USD_TO_CNY_RATE)
export const StripeFee = process.env.STRIPE_FEE && parseFloat(process.env.STRIPE_FEE)
export const SyncApiBaseURL = process.env.SYNC_API_BASE_URL || '/api'
export const SyncAuthBaseURL = process.env.SYNC_AUTH_BASE_URL || '/auth'
export const SyncFilesBaseURL = process.env.SYNC_FILES_BASE_URL || '/files'
export const SyncWsURL = process.env.SYNC_WS_URL || '/ws'
export const SyncEnabled = (process.env.SYNC_ENABLED ?? 'true') !== 'false'
export const LitellmBaseURL = process.env.LITELLM_BASE_URL
export const BudgetBaseURL = process.env.BUDGET_BASE_URL
export const SearxngBaseURL = process.env.SEARXNG_BASE_URL
export const DisableCheckUpdate = process.env.DISABLE_CHECK_UPDATE === 'true'
