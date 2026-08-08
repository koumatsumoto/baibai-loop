import { clearViewPassword, getViewPassword, notifyAuthRequired } from './auth'

const API_PREFIX = '/api/'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, statusText: string) {
    super(`API request failed: ${status} ${statusText}`)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  // Compose the caller's headers with the JSON Accept header; only Accept is forced.
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')

  if (path.startsWith(API_PREFIX)) {
    const password = getViewPassword()
    if (password !== null && password !== '') {
      try {
        headers.set('Authorization', `Bearer ${password}`)
      } catch {
        // A stored value that is not a valid Latin-1 header throws here, before the
        // request. Drop it and hand control to the gate so it self-recovers instead of
        // dead-ending every request with a pre-flight error.
        clearViewPassword()
        notifyAuthRequired('rejected')
        throw new ApiError(401, 'Stored view password is not a valid header value')
      }
    }
  }

  const response = await fetch(path, { ...init, headers })
  // A 401 means the stored password is absent or rejected: drop it and hand control to
  // the password gate. Local FastAPI never returns 401, so this path stays dormant.
  if (response.status === 401 && path.startsWith(API_PREFIX)) {
    const hadPassword = getViewPassword() !== null
    clearViewPassword()
    notifyAuthRequired(hadPassword ? 'rejected' : 'required')
    throw new ApiError(response.status, response.statusText)
  }
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText)
  }
  return (await response.json()) as T
}
