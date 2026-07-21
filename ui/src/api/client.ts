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

// Compose the caller's headers with the JSON Accept header and, for `/api/` reads,
// the stored view password as a Bearer token. The caller's headers are preserved;
// only Accept is forced.
function buildHeaders(path: string, init?: RequestInit): Headers {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  if (path.startsWith(API_PREFIX)) {
    const password = getViewPassword()
    if (password !== null && password !== '') {
      headers.set('Authorization', `Bearer ${password}`)
    }
  }
  return headers
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: buildHeaders(path, init),
  })
  // A 401 means the stored password is absent or rejected: drop it and hand control
  // to the password gate. Local FastAPI never returns 401, so this path stays dormant.
  if (response.status === 401 && path.startsWith(API_PREFIX)) {
    clearViewPassword()
    notifyAuthRequired()
    throw new ApiError(response.status, response.statusText)
  }
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText)
  }
  return (await response.json()) as T
}
