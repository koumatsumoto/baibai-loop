export class ApiError extends Error {
  readonly status: number

  constructor(status: number, statusText: string) {
    super(`API request failed: ${status} ${statusText}`)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText)
  }
  return (await response.json()) as T
}
