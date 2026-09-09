// Research工程の閲覧資格情報をowner永続保存とshared sessionに分けて保持する。
const VIEW_PASSWORD_KEY = 'baibai-view-password'

export function getViewPassword(): string | null {
  return localStorage.getItem(VIEW_PASSWORD_KEY)
}

export function setViewPassword(value: string): void {
  localStorage.setItem(VIEW_PASSWORD_KEY, value)
}

export function clearViewPassword(): void {
  localStorage.removeItem(VIEW_PASSWORD_KEY)
}

// `rejected`: a stored password was present and got cleared (a 401 answer or a value the
// request layer refuses). `required`: no stored password, so this is a first prompt. The
// gate uses this to distinguish a repeat prompt from the initial one.
export type AuthRequiredReason = 'required' | 'rejected'

type AuthRequiredListener = (reason: AuthRequiredReason) => void

const authRequiredListeners = new Set<AuthRequiredListener>()

/** Subscribe to the auth-required signal; returns an unsubscribe function. */
export function subscribeAuthRequired(listener: AuthRequiredListener): () => void {
  authRequiredListeners.add(listener)
  return () => {
    authRequiredListeners.delete(listener)
  }
}

/** Fire the auth-required signal so the password gate takes over. */
export function notifyAuthRequired(reason: AuthRequiredReason): void {
  for (const listener of authRequiredListeners) listener(reason)
}

const SHARED_TOKEN_KEY = 'baibai-shared-read-token'

export function getSharedToken(): string | null {
  return sessionStorage.getItem(SHARED_TOKEN_KEY)
}

export function clearSharedToken(): void {
  sessionStorage.removeItem(SHARED_TOKEN_KEY)
}

/** Consume sharing credentials before rendering can trigger API requests. */
export function bootstrapSharedAccess(): void {
  const url = new URL(window.location.href)
  const tokens = url.searchParams.getAll('share')
  if (tokens.length === 0) return
  url.searchParams.delete('share')
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  clearSharedToken()
  if (tokens.length === 1 && tokens[0]) sessionStorage.setItem(SHARED_TOKEN_KEY, tokens[0])
}
