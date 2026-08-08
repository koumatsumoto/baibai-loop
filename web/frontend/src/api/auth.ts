// View-password storage and the auth-required signal, kept in one place so the fetch
// client and the password gate share a single source. The password authorises
// `/api/` reads behind a fixed Bearer scheme; locally no password is stored, so the
// header is never sent and the signal never fires.

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
