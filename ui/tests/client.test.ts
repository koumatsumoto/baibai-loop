import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, fetchJson } from '../src/api/client'
import { getViewPassword, setViewPassword, subscribeAuthRequired } from '../src/api/auth'

function createLocalStorage(): Storage {
  const store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    removeItem: (key: string) => {
      store.delete(key)
    },
    setItem: (key: string, value: string) => {
      store.set(key, value)
    },
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

let fetchMock: ReturnType<typeof vi.fn>

function requestHeaders(): Headers {
  const init = fetchMock.mock.calls[0]?.[1] as RequestInit
  return init.headers as Headers
}

beforeEach(() => {
  vi.stubGlobal('localStorage', createLocalStorage())
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchJson headers', () => {
  it('keeps caller-supplied headers while forcing the JSON Accept header', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await fetchJson('/api/dashboard', { headers: { 'X-Trace-Id': 'trace-123' } })
    const headers = requestHeaders()
    expect(headers.get('X-Trace-Id')).toBe('trace-123')
    expect(headers.get('Accept')).toBe('application/json')
  })

  it('adds a bearer Authorization header when a view password is stored', async () => {
    const password = 'stored-view-secret'
    setViewPassword(password)
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await fetchJson('/api/dashboard')
    expect(requestHeaders().get('Authorization')).toBe(`Bearer ${password}`)
  })

  it('sends no Authorization header when no password is stored (local default)', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await fetchJson('/api/dashboard')
    expect(requestHeaders().get('Authorization')).toBeNull()
  })
})

describe('fetchJson 401 handling', () => {
  it('clears the stored password and signals it was rejected on a 401 from /api/', async () => {
    setViewPassword('rejected-secret')
    const listener = vi.fn()
    const unsubscribe = subscribeAuthRequired(listener)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401))
    try {
      await expect(fetchJson('/api/dashboard')).rejects.toBeInstanceOf(ApiError)
      expect(getViewPassword()).toBeNull()
      expect(listener).toHaveBeenCalledTimes(1)
      expect(listener).toHaveBeenCalledWith('rejected')
    } finally {
      unsubscribe()
    }
  })

  it('signals a first prompt (required) on a 401 with no stored password', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeAuthRequired(listener)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401))
    try {
      await expect(fetchJson('/api/dashboard')).rejects.toBeInstanceOf(ApiError)
      expect(listener).toHaveBeenCalledWith('required')
    } finally {
      unsubscribe()
    }
  })
})

describe('fetchJson non-Latin-1 password self-recovery', () => {
  it('drops an unusable stored password and triggers the gate before any request', async () => {
    // Header values are Latin-1 ByteStrings; a non-ASCII value makes Headers.set throw
    // before the request. The client must recover, not dead-end.
    setViewPassword('ひみつ')
    const listener = vi.fn()
    const unsubscribe = subscribeAuthRequired(listener)
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    try {
      await expect(fetchJson('/api/dashboard')).rejects.toBeInstanceOf(ApiError)
      expect(getViewPassword()).toBeNull()
      expect(listener).toHaveBeenCalledWith('rejected')
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      unsubscribe()
    }
  })
})
