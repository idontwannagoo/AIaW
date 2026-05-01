export const MaxMessageFileSizeMB = parseFloat(process.env.MAX_MESSAGE_FILE_SIZE_MB || '20')
export const DocParseBaseURL = process.env.DOC_PARSE_BASE_URL
export const CorsFetchBaseURL = process.env.CORS_FETCH_BASE_URL
export const SyncServicePrice = process.env.SYNC_SERVICE_PRICE && parseFloat(process.env.SYNC_SERVICE_PRICE)
export const SyncServicePriceUSD = process.env.SYNC_SERVICE_PRICE_USD && parseFloat(process.env.SYNC_SERVICE_PRICE_USD)
export const UsdToCnyRate = process.env.USD_TO_CNY_RATE && parseFloat(process.env.USD_TO_CNY_RATE)
export const StripeFee = process.env.STRIPE_FEE && parseFloat(process.env.STRIPE_FEE)
export const DexieDBURL = process.env.DEXIE_DB_URL
// Stage 1+ self-hosted backend. Empty = no backend, behave like Stage 0.
export const BackendApiBaseURL = process.env.BACKEND_DATA_API_URL
// Stage 1.5: when 'true', authSource is BackendAuthSource (self-hosted JWT).
// Otherwise the DexieAuthSource is used. Independent from BACKEND_DATA_API_URL
// because UI may want to log into backend without yet routing data to it.
export const BackendAuth = process.env.BACKEND_AUTH === 'true'
export const LitellmBaseURL = process.env.LITELLM_BASE_URL
export const BudgetBaseURL = process.env.BUDGET_BASE_URL
export const SearxngBaseURL = process.env.SEARXNG_BASE_URL
export const DisableCheckUpdate = process.env.DISABLE_CHECK_UPDATE === 'true'
