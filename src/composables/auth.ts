import { syncClient } from 'src/utils/sync-client'

export function useAuth() {
  return {
    state: syncClient.state,
    login: syncClient.login,
    register: syncClient.register,
    logout: syncClient.logout
  }
}
