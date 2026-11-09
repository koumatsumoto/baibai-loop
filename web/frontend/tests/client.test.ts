import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, fetchJson } from '../src/api/client'
import { clearViewPassword, getViewPassword, setViewPassword, subscribeAuthRequired } from '../src/api/auth'

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
  setViewPassword('')
  clearViewPassword()
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

  it.each(['/api/dashboard', '/api/proposal/latest'])('adds the existing bearer password for %s', async path => {
    const password = 'stored-view-secret'
    setViewPassword(password)
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await fetchJson(path)
    expect(requestHeaders().get('Authorization')).toBe(`Bearer ${password}`)
  })

  it('sends no Authorization header when no password is stored (local default)', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await fetchJson('/api/dashboard')
    expect(requestHeaders().get('Authorization')).toBeNull()
  })
})

describe('fetchJson 401 handling', () => {
  it.each(['/api/dashboard', '/api/proposal/latest'])('clears the rejected password and reopens the gate for %s', async path => {
    setViewPassword('rejected-secret')
    const listener = vi.fn()
    const unsubscribe = subscribeAuthRequired(listener)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401))
    try {
      await expect(fetchJson(path)).rejects.toBeInstanceOf(ApiError)
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

describe('obsolete 401 responses', () => {
  it.each(['owner', 'none'] as const)('does not relock a new owner after a %s request', async (source) => {
    if (source === 'owner') setViewPassword('old-secret')
    let respond!: (response: Response) => void
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => { respond = resolve }))
    const listener = vi.fn()
    const unsubscribe = subscribeAuthRequired(listener)
    try {
      const pending = fetchJson('/api/dashboard')
      setViewPassword('new-secret')
      respond(jsonResponse({}, 401))
      await expect(pending).rejects.toMatchObject({ status: 401 })
      expect(listener).not.toHaveBeenCalled()
      expect(getViewPassword()).toBe('new-secret')
    } finally {
      unsubscribe()
    }
  })


})


describe('auth signal before the password gate subscribes', () => {
  it('replays a synchronous invalid-header rejection after initial mounting', async () => {
    setViewPassword('ひみつ')
    await expect(fetchJson('/api/dashboard')).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock).not.toHaveBeenCalled()
    const listener = vi.fn()
    const unsubscribe = subscribeAuthRequired(listener)
    try {
      expect(listener).toHaveBeenCalledExactlyOnceWith('rejected')
    } finally {
      unsubscribe()
    }
  })

  it('keeps a required prompt through resubscription, but clears it for a new submit', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, 401))
    await expect(fetchJson('/api/dashboard')).rejects.toBeInstanceOf(ApiError)
    const first = vi.fn()
    subscribeAuthRequired(first)()
    expect(first).toHaveBeenCalledExactlyOnceWith('required')
    const second = vi.fn()
    subscribeAuthRequired(second)()
    expect(second).toHaveBeenCalledExactlyOnceWith('required')
    setViewPassword('new-view-secret')
    const afterSubmit = vi.fn()
    subscribeAuthRequired(afterSubmit)()
    expect(afterSubmit).not.toHaveBeenCalled()
  })
})
