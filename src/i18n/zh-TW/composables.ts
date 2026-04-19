export default {
  firstVisit: {
    title: '歡迎使用 AI as Workspace',
    messageWithLogin: '為了使用 AI 模型，你需要手動<b>設定服務商</b>，或<b>登入</b>以使用我們提供的模型服務。 <br>此外，登入之後還可以使用跨裝置即時雲端同步功能。',
    messageWithoutLogin: '為了使用 AI 模型，你需要手動<b>設定服務商</b>，或<b>登入</b>以使用我們提供的模型服務',
    cancel: '配置服務商',
    ok: '登入'
  },
  createDialog: {
    newDialog: '新對話'
  },
  callApi: {
    argValidationFailed: '呼叫參數校驗失敗',
    settingsValidationFailed: '插件設定校驗失敗，請檢查插件設定'
  },
  order: {
    failure: '下單失敗'
  },
  login: {
    loginTitle: '登入',
    registerTitle: '建立帳號',
    emailLabel: '電子郵件',
    passwordLabel: '密碼',
    login: '登入',
    register: '註冊',
    cancel: '取消',
    switchToLogin: '已有帳號？去登入',
    switchToRegister: '沒有帳號？去註冊',
    invalidEmail: '請輸入有效的電子郵件地址',
    invalidPassword: '密碼至少需要6個字元',
    emailAlreadyRegistered: '此電子郵件已被註冊',
    invalidCredentials: '電子郵件或密碼不正確',
    unknownError: '發生未知錯誤',
    logout: '登出',
    confirmLogout: '確定要登出嗎？',
    loggedIn: '已登入：{email}',
    privacyPolicy: '註冊即代表同意我們的<a href="https://docs.aiaw.app/zh/privacy-policy/" text-pri target="_blank">隱私政策</a>'
  },
  installPlugin: {
    fetchFailed: '獲取插件配置失敗：{message}',
    installFailed: '插件安裝失敗：{message}',
    formatError: '格式錯誤',
    unsupportedFormat: '不支援的插件格式'
  },
  workspace: {
    newWorkspace: '新建工作區',
    name: '名稱',
    create: '建立',
    defaultAssistant: '預設助手',
    newFolder: '新建資料夾',
    rename: '重新命名',
    deleteWorkspace: '刪除工作區',
    deleteFolder: '刪除資料夾',
    confirmDeleteWorkspace: '確定要刪除工作區「{name}」嗎？其內部的所有對話和助手都將被刪除',
    confirmDeleteFolder: '確定要刪除資料夾「{name}」嗎？其內部的所有工作區都將被刪除',
    delete: '刪除'
  },
  subscriptionNotify: {
    evalExpired: '雲同步試用已過期，可選擇訂閱',
    evalExpiring: '雲同步試用即將過期，可選擇訂閱',
    prodExpired: '您的雲同步訂閱已過期',
    prodExpiring: '您的雲同步訂閱即將過期',
    renewal: '續訂',
    subscribe: '訂閱'
  }
}
